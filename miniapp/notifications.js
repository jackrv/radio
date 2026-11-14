"use strict";

(() => {

  const telegram = window.Telegram?.WebApp;
  const fieldset = document.getElementById("notify-mode-fieldset");
  const toggle = document.getElementById("silence-toggle");
  const label = document.getElementById("notification-mode-label");
  const detail = document.getElementById("notification-mode-detail");
  const settingsValue = document.getElementById("settings-mode-value");
  const feedback = document.getElementById("notify-mode-feedback");
  const radios = Array.from(
    document.querySelectorAll('input[name="notify-mode"]')
  );

  const names = {
    all: "Усі повідомлення",
    watch: "Список спостереження",
    commands: "Команди й позивні",
    off: "Повна тиша"
  };

  let currentMode = null;
  let previousMode = "all";
  let isAdmin = false;
  let busy = false;
  let generation = 0;

  function message(text, error = false) {
    feedback.textContent = text;
    feedback.style.color = error ? "#ff9b9b" : "#a3c0b2";
  }

  function render() {
    const name = names[currentMode] || "Невідомо";

    label.textContent = name;
    settingsValue.textContent = name;

    detail.textContent = currentMode === "off"
      ? "Автоматичні сповіщення вимкнено. Приймання триває."
      : currentMode
        ? "Режим застосовується до нових сповіщень."
        : "Немає даних від API.";

    toggle.textContent = currentMode === "off"
      ? "Увімкнути"
      : "Вимкнути";

    const disabled = !isAdmin || busy || !currentMode;

    toggle.disabled = disabled;
    fieldset.disabled = disabled;

    for (const radio of radios) {
      radio.checked = radio.value === currentMode;
    }
  }

  async function request(method, path, data) {
    return window.Radio345.request(path, method, data);
  }

  function applyMode(mode) {
    if (!(mode in names)) {
      throw new Error("Сервер повернув невідомий режим.");
    }

    currentMode = mode;

    if (mode !== "off") {
      previousMode = mode;
    }

    render();
  }

  async function refreshMode() {
    if (!isAdmin || busy || document.visibilityState === "hidden") {
      return;
    }

    const expectedGeneration = generation;

    try {
      const data = await request(
        "GET", "/api/notifications/mode"
      );

      // Не перезаписываем результат более нового PUT.
      if (expectedGeneration !== generation || busy) {
        return;
      }

      applyMode(data.mode);
      message("");
    } catch (error) {
      if (expectedGeneration === generation && !busy) {
        message(`Не вдалося оновити режим: ${error.message}`, true);
      }
    }
  }

  async function updateMode(mode) {
    if (!isAdmin || busy || !(mode in names)) {
      return;
    }

    if (mode === currentMode) {
      return;
    }

    generation++;
    busy = true;
    render();
    message("Зберігаємо налаштування...");

    try {
      const result = await request(
        "PUT",
        "/api/notifications/mode",
        { mode }
      );

      applyMode(result.mode);
      message("Налаштування збережено.");
    } catch (error) {
      message(`Помилка збереження: ${error.message}`, true);
    } finally {
      busy = false;
      render();
    }
  }

  toggle.addEventListener("click", () => {
    const target = currentMode === "off" ? previousMode : "off";
    updateMode(target);
  });

  for (const radio of radios) {
    radio.addEventListener("change", () => {
      if (radio.checked) {
        updateMode(radio.value);
      }
    });
  }

  async function start() {
    render();

    if (!telegram?.initData) {
      message("Відкрийте панель через Telegram.");
      return;
    }

    try {
      const user = await window.Radio345.user();
      isAdmin = user.role === "admin";

      if (!isAdmin) {
        label.textContent = "Доступ адміністратора";
        detail.textContent =
          "Зміна сповіщень доступна лише адміністратору.";
        settingsValue.textContent = "Немає доступу";
        message("Керування доступне лише адміністратору.");
        return;
      }

      await refreshMode();

      window.setInterval(refreshMode, 15000);

      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") {
          refreshMode();
        }
      });
    } catch (error) {
      label.textContent = "Помилка підключення";
      detail.textContent = error.message;
      settingsValue.textContent = "Недоступно";
      message(error.message, true);
    }
  }

  start();
})();
