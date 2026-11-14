"use strict";

// Екран стану сервісу: замість заглушок панелі, доки API не відповів.
(() => {
  const RETRY_MS = 10000;
  const shell = document.querySelector(".shell");
  const gate = document.getElementById("service-gate");
  const title = document.getElementById("service-gate-title");
  const text = document.getElementById("service-gate-text");
  const note = document.getElementById("service-gate-note");
  const retry = document.getElementById("service-gate-retry");
  const banner = document.getElementById("connection-lost");

  const states = {
    connecting: ["Підключення до Radio 345…", "Перевіряємо зв’язок із сервісом.", false],
    unavailable: [
      "Сервіс Radio 345 недоступний",
      "Приймач на Mac вимкнено або немає зв’язку з ним. Панель відкриється сама, щойно сервіс запрацює.",
      true,
    ],
    telegram: ["Відкрийте через Telegram", "Панель працює лише з бота Radio 345 у Telegram.", false],
    expired: ["Сеанс застарів", "Закрийте й знову відкрийте Mini App.", false],
    forbidden: [
      "Немає доступу",
      "Ваш обліковий запис не має доступу до Radio 345. Надішліть боту /start, щоб попросити доступ.",
      false,
    ],
  };

  let ready = false;
  let wasUnavailable = false;
  let timer = null;
  let checking = false;

  function show(state) {
    const [heading, body, canRetry] = states[state];
    title.textContent = heading;
    text.textContent = body;
    retry.hidden = !canRetry;
    note.hidden = !canRetry;
    note.textContent = canRetry ? `Автоматична перевірка кожні ${RETRY_MS / 1000} с.` : "";
    gate.hidden = false;
    shell.hidden = true;
  }

  function open() {
    ready = true;
    gate.hidden = true;
    shell.hidden = false;
  }

  async function check() {
    if (checking) return;
    checking = true;
    window.clearTimeout(timer);
    try {
      await window.Radio345.user();
      // Після недоступності — чистий запуск усіх екранів.
      if (wasUnavailable) {
        window.location.reload();
        return;
      }
      open();
    } catch (error) {
      if (error.status === 401) return show("expired");
      if (error.status === 403) return show("forbidden");
      wasUnavailable = true;
      show("unavailable");
      timer = window.setTimeout(check, RETRY_MS);
    } finally {
      checking = false;
    }
  }

  retry.addEventListener("click", () => {
    show("connecting");
    check();
  });

  // Під час роботи панель не ховаємо: незбережений текст редактора лишається.
  window.Radio345.onConnection(reachable => {
    if (ready) banner.hidden = reachable;
  });

  if (!window.Telegram?.WebApp?.initData) {
    show("telegram");
    return;
  }
  show("connecting");
  check();
})();
