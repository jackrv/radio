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
    all: "Все сообщения",
    watch: "Список наблюдения",
    commands: "Команды и позывные",
    off: "Полная тишина"
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
    const name = names[currentMode] || "Неизвестно";

    label.textContent = name;
    settingsValue.textContent = name;

    detail.textContent = currentMode === "off"
      ? "Автоматические уведомления отключены. Приём продолжается."
      : currentMode
        ? "Режим применяется к новым уведомлениям."
        : "Нет данных от API.";

    toggle.textContent = currentMode === "off"
      ? "Включить"
      : "Выключить";

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
      throw new Error("Сервер вернул неизвестный режим.");
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
        message(`Не удалось обновить режим: ${error.message}`, true);
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
    message("Сохраняем настройку...");

    try {
      const result = await request(
        "PUT",
        "/api/notifications/mode",
        { mode }
      );

      applyMode(result.mode);
      message("Настройка сохранена.");
    } catch (error) {
      message(`Ошибка сохранения: ${error.message}`, true);
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
      message("Откройте панель через Telegram.");
      return;
    }

    try {
      const user = await window.Radio345.user();
      isAdmin = user.role === "admin";

      if (!isAdmin) {
        label.textContent = "Доступ администратора";
        detail.textContent =
          "Изменение уведомлений доступно только администратору.";
        settingsValue.textContent = "Нет доступа";
        message("Управление доступно только администратору.");
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
      label.textContent = "Ошибка подключения";
      detail.textContent = error.message;
      settingsValue.textContent = "Недоступно";
      message(error.message, true);
    }
  }

  start();
})();
