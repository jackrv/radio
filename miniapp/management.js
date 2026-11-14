"use strict";

(() => {
  const { request, user } = window.Radio345;
  const $ = id => document.getElementById(id);
  let me = null;
  const canEdit = () => ["admin", "editor"].includes(me?.role);
  const isAdmin = () => me?.role === "admin";
  const roles = { viewer: "Наблюдатель", editor: "Редактор", admin: "Администратор" };
  const states = { active: "Активно", pending: "Ожидает подтверждений / проверка конфликтов", disabled: "Отключено" };
  const loaded = new Set();
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
    return value ? new Date(value.replace(" ", "T") + "Z").toLocaleString("ru-RU") : "—";
  }

  async function overview() {
    try {
      const data = await request("/api/overview");
      $("events-count").textContent = data.events;
      $("rules-count").textContent = data.active_rules;
      $("dictionary-count").textContent = data.dictionary_entries;
      message("overview-feedback", `Последнее событие: ${localDate(data.last_event_at)} · Твой Telegram ID: ${me.user_id}`);
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
      message(prefix === "rules" ? "dictionary-feedback" : "phrases-feedback", "Загрузка...");
      try {
        const data = await request(`${path}?q=${encodeURIComponent(query)}&offset=${offset}&limit=20`);
        if (prefix === "phrases") releaseAudio();
        $(prefix + "-list").replaceChildren();
        for (const item of data.items) $(prefix + "-list").append(render(item));
        if (!data.items.length) $(prefix + "-list").append(node("p", "Записей нет.", "subtle-note"));
        $(prefix + "-prev").disabled = offset === 0;
        $(prefix + "-next").disabled = !data.has_more;
        message(prefix === "rules" ? "dictionary-feedback" : "phrases-feedback", `Страница ${offset / 20 + 1}`);
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
    $(prefix + "-prev").addEventListener("click", () => { if (!busy) { offset = Math.max(0, offset - 20); load(); } });
    $(prefix + "-next").addEventListener("click", () => { if (!busy) { offset += 20; load(); } });
    return load;
  }

  const phrases = pagedList("phrases", "/api/phrases", item => {
    const card = node("article", undefined, "data-card");
    card.append(node("div", `${localDate(item.created_at)} · ${item.fix_id.startsWith("mem:") ? "Временная фраза" : item.fix_id}`, "subtle-note"));
    card.append(node("p", item.current_text, "phrase-text"));
    if (item.original_text !== item.current_text) card.append(node("p", `Исходный текст: ${item.original_text}`, "subtle-note phrase-text"));
    if (item.has_audio) {
      const listen = button("▶ Прослушать аудио", async () => {
        listen.disabled = true;
        listen.textContent = "Загружаем аудио...";
        try {
          const blob = await request(`/api/phrases/audio?fix_id=${encodeURIComponent(item.fix_id)}`, "GET", undefined, "blob");
          if (!card.isConnected) return;
          const player = node("audio");
          player.controls = true;
          const url = URL.createObjectURL(blob);
          audioUrls.add(url);
          player.src = url;
          player.addEventListener("error", () => {
            message("phrases-feedback", "Устройство не воспроизводит OGG. Прослушай исходное голосовое в Telegram.", true);
            URL.revokeObjectURL(url); audioUrls.delete(url);
          });
          card.append(player);
          listen.hidden = true;
        } catch (error) {
          message("phrases-feedback", error.message, true);
          listen.disabled = false; listen.textContent = "▶ Повторить загрузку аудио";
        }
      });
      card.append(listen);
    } else {
      card.append(node("p", "Аудиозапись не привязана к этой фразе.", "subtle-note"));
    }
    if (canEdit()) card.append(button("Исправить", () => {
      const url = new URL(window.location.href);
      url.searchParams.set("fix_id", item.fix_id);
      window.location.assign(url.toString());
    }));
    return card;
  });

  const rules = pagedList("rules", "/api/word-rules", item => {
    const card = node("article", undefined, "data-card");
    card.append(node("p", `${item.original} → ${item.corrected}`, "phrase-text"));
    card.append(node("p", `${states[item.status]} · Подтверждений: ${item.confirmations}`, "subtle-note"));
    if (canEdit()) {
      const toggle = button(item.status === "disabled" ? "Включить с проверкой" : "Отключить", async () => {
        toggle.disabled = true;
        try {
          await request(`/api/word-rules/${item.id}`, "PUT", { disabled: item.status !== "disabled", expected_version: item.version });
          await rules();
        } catch (error) { message("dictionary-feedback", error.message, true); }
        finally { toggle.disabled = false; }
      });
      card.append(toggle);
    }
    return card;
  });

  let dictionary = null;
  let dictionaryBusy = false;
  let dirty = false;
  let selectedKind = "callsigns";
  function renderDictionary() {
    const list = $("dictionary-entries");
    list.replaceChildren();
    dictionary.entries.forEach((entry, index) => {
      const row = node("div", undefined, "dictionary-row");
      const enabled = node("input");
      enabled.type = "checkbox"; enabled.checked = entry.enabled; enabled.disabled = !canEdit() || dictionaryBusy;
      enabled.setAttribute("aria-label", `Строка ${index + 1} включена`);
      enabled.addEventListener("change", () => { entry.enabled = enabled.checked; dirty = true; });
      const text = node("input");
      text.value = entry.raw_line; text.maxLength = 500; text.disabled = !canEdit() || dictionaryBusy;
      text.setAttribute("aria-label", `Текст строки ${index + 1}`);
      text.addEventListener("input", () => { entry.raw_line = text.value; dirty = true; });
      const remove = button("Удалить", () => { dictionary.entries.splice(index, 1); dirty = true; renderDictionary(); });
      remove.disabled = !canEdit() || dictionaryBusy;
      row.append(enabled, text, remove); list.append(row);
    });
    if (!dictionary.entries.length) list.append(node("p", "Словарь пуст. Добавь первую строку.", "subtle-note"));
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
    message("dictionary-feedback", "Загрузка...");
    try {
      dictionary = await request(`/api/dictionaries/${kind}`);
      dirty = false;
      renderDictionary();
      message("dictionary-feedback", canEdit() ? "Изменения применятся после перезапуска приёмника." : "Режим просмотра. Для правок нужна роль редактора.");
    } catch (error) { message("dictionary-feedback", error.message, true); }
    finally {
      dictionaryBusy = false;
      $("dictionary-kind").disabled = false;
      $("dictionary-save").disabled = $("dictionary-add").disabled = !canEdit() || !dictionary;
      if (dictionary) renderDictionary();
    }
  }
  $("dictionary-kind").addEventListener("change", () => {
    if (dirty && !window.confirm("Не сохранять изменения словаря?")) { $("dictionary-kind").value = selectedKind; return; }
    loadDictionary();
  });
  $("dictionary-refresh").addEventListener("click", () => {
    if (!dirty || window.confirm("Обновить словарь и потерять несохранённые правки?")) loadDictionary();
  });
  $("dictionary-add").addEventListener("click", () => {
    if (!dictionary || dictionaryBusy || !canEdit()) return;
    dictionary.entries.push({ raw_line: "", enabled: true }); dirty = true; renderDictionary();
  });
  $("dictionary-save").addEventListener("click", async () => {
    if (!dictionary || dictionaryBusy || !canEdit()) return;
    dictionaryBusy = true;
    $("dictionary-save").disabled = $("dictionary-add").disabled = $("dictionary-kind").disabled = true;
    renderDictionary();
    try {
      dictionary = await request(`/api/dictionaries/${dictionary.kind}`, "PUT", { entries: dictionary.entries, expected_version: dictionary.version });
      dirty = false;
      message("dictionary-feedback", "Сохранено. Перезапусти приёмник для применения словаря.");
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
    if (!isAdmin()) { message("recognition-feedback", "Настройки доступны только администратору."); return; }
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
      $("recognition-models").textContent = `Whisper: ${data.whisper_model} · Qwen: ${data.correction_model} · Язык: ${data.language}`;
      $("recognition-fields").disabled = false;
      message("recognition-feedback", "Сохранённые значения для следующего запуска приёмника.");
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
      message("recognition-feedback", "Сохранено. Перезапусти приёмник, чтобы применить настройки.");
    } catch (error) { message("recognition-feedback", error.message + " Загрузи сохранённые настройки перед новой правкой.", true); }
    finally { settingsBusy = false; $("recognition-fields").disabled = false; }
  });
  $("recognition-refresh").addEventListener("click", () => {
    if (!settingsBusy && window.confirm("Загрузить сохранённые настройки и заменить значения в форме?")) settings();
  });
  $("overview-refresh").addEventListener("click", () => { if (me) overview(); });

  let usersBusy = false;
  async function users() {
    if (!isAdmin()) { message("users-feedback", "Управление пользователями доступно только администратору."); return; }
    try {
      const data = await request("/api/users");
      $("users-list").replaceChildren();
      for (const item of data.items) {
        const card = node("article", undefined, "data-card");
        card.append(node("h2", `Telegram ${item.user_id}`));
        const row = node("div", undefined, "toolbar");
        const role = node("select");
        role.setAttribute("aria-label", `Роль ${item.user_id}`);
        for (const [key, name] of Object.entries(roles)) { const option = node("option", name); option.value = key; role.append(option); }
        role.value = item.role;
        const label = node("label", undefined, "checkbox-label");
        const enabled = node("input"); enabled.type = "checkbox"; enabled.checked = item.enabled;
        label.append(enabled, node("span", "Доступ включён"));
        const save = button("Сохранить", async () => {
          if (usersBusy || !window.confirm(`Изменить доступ пользователя ${item.user_id}?`)) return;
          await saveUser(item.user_id, { role: role.value, enabled: enabled.checked, expected_version: item.version });
        });
        row.append(role, label, save); card.append(row); $("users-list").append(card);
      }
      $("user-add-fields").disabled = false;
      message("users-feedback", "Последнего активного администратора отключить нельзя.");
    } catch (error) { message("users-feedback", error.message, true); }
  }
  async function saveUser(id, payload) {
    if (!isAdmin() || usersBusy) return;
    usersBusy = true;
    $("user-add-fields").disabled = true;
    try {
      await request(`/api/users/${id}`, "PUT", payload);
      $("new-user-id").value = "";
      if (String(me.user_id) === id && (payload.role !== "admin" || !payload.enabled)) {
        $("users-list").replaceChildren();
        message("users-feedback", "Твой доступ изменён. Закрой и заново открой панель.");
        me = null;
        return;
      }
      await users();
      message("users-feedback", "Доступ сохранён.");
    } catch (error) { message("users-feedback", error.message, true); }
    finally { usersBusy = false; $("user-add-fields").disabled = !isAdmin(); }
  }
  $("user-add-form").addEventListener("submit", event => {
    event.preventDefault();
    const id = $("new-user-id").value.trim();
    if (!/^[1-9][0-9]{0,18}$/.test(id)) return;
    if (!window.confirm(`Предоставить доступ Telegram ID ${id}?`)) return;
    saveUser(id, { role: $("new-user-role").value, enabled: true, expected_version: null });
  });
  $("users-refresh").addEventListener("click", () => { if (!usersBusy) users(); });

  const loaders = { home: overview, corrections: phrases, dictionaries: loadDictionary, settings, users };
  async function loadView(name) {
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
      await overview();
      const current = document.querySelector(".view.active");
      if (current) await loadView(current.id.replace("view-", ""));
    } catch (error) {
      for (const id of ["overview-feedback", "phrases-feedback", "dictionary-feedback", "recognition-feedback", "users-feedback"]) message(id, error.message, true);
    }
  }
  start();
})();
