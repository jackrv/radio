"use strict";

(() => {

  const telegram = window.Telegram?.WebApp;
  const fieldset = document.getElementById("notify-mode-fieldset");
  const label = document.getElementById("notification-mode-label");
  const feedback = document.getElementById("notify-mode-feedback");
  const radios = Array.from(
    document.querySelectorAll('input[name="notify-mode"]')
  );

  const names = {
    all: "Усі",
    watch: "Спостереження",
    commands: "Команди",
    off: "Тиша"
  };

  let currentMode = null;
  let ready = false;
  let allowed = [];
  let busy = false;
  let generation = 0;

  function message(text, error = false) {
    feedback.textContent = text;
    feedback.style.color = error ? "#ff9b9b" : "#a3c0b2";
  }

  function render() {
    label.textContent = names[currentMode] || "—";
    fieldset.disabled = !ready || busy || !currentMode;

    for (const radio of radios) {
      radio.checked = radio.value === currentMode;
      // «Усі повідомлення» доступні лише адміністратору; сервер перевіряє теж.
      radio.disabled = !allowed.includes(radio.value);
      const option = radio.closest(".notify-option");
      option?.toggleAttribute("aria-disabled", radio.disabled);
      // Недоступний ролі режим не показуємо, щойно відомо, що дозволено.
      if (option && allowed.length) option.hidden = radio.disabled;
    }
  }

  async function request(method, path, data) {
    return window.Radio345.request(path, method, data);
  }

  function applyMode(mode, allowedModes) {
    if (!(mode in names)) {
      throw new Error("Сервер повернув невідомий режим.");
    }

    currentMode = mode;
    if (Array.isArray(allowedModes)) {
      allowed = allowedModes.filter((value) => value in names);
    }

    render();
  }

  async function refreshMode() {
    if (!ready || busy || document.visibilityState === "hidden") {
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

      applyMode(data.mode, data.allowed);
      message(currentMode === "off" ? "Сповіщення вимкнено лише для вас. Приймання триває." : "");
    } catch (error) {
      if (expectedGeneration === generation && !busy) {
        message(`Не вдалося оновити режим: ${error.message}`, true);
      }
    }
  }

  async function updateMode(mode) {
    if (!ready || busy || !(mode in names) || !allowed.includes(mode)) {
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

      applyMode(result.mode, result.allowed);
      message(result.mode === "off" ? "Сповіщення вимкнено лише для вас. Приймання триває." : "Збережено.");
    } catch (error) {
      message(`Помилка збереження: ${error.message}`, true);
    } finally {
      busy = false;
      render();
    }
  }

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
      if (user.role === "viewer") return;
      ready = true;

      await refreshMode();

      window.setInterval(refreshMode, 15000);

      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") {
          refreshMode();
        }
      });
    } catch (error) {
      label.textContent = "Недоступно";
      message(error.message, true);
    }
  }

  start();
})();
