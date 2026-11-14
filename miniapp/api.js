"use strict";

(() => {
  const telegram = window.Telegram?.WebApp;

  const pill = document.getElementById("connection-pill");
  const receiver = document.getElementById("receiver-status");
  const updated = document.getElementById("status-updated");
  const hero = document.querySelector(".hero-live");
  const description = document.querySelector(".hero-caption");
  const platform = document.getElementById("platform-label");

  let expired = false;
  let busy = false;

  function connection(text, online = false) {
    pill.textContent = text;
    pill.style.background = online ? "#1d5745" : "#26303a";
    pill.style.color = online ? "#a5f3cf" : "#b9c5ce";
  }

  function showReceiver(state) {
    const listening = state === "listening";
    const paused = state === "paused";

    receiver.textContent = listening
      ? "Слухає ефір"
      : paused ? "Приймання призупинено" : "Приймання зупинено";

    hero.textContent = listening
      ? "● Приймання активне"
      : paused ? "● Мікрофон закрито" : "● Немає активного приймання";

    hero.style.background = listening
      ? "#36564e" : paused ? "#60502d" : "#33404e";

    description.textContent = listening
      ? "Аудіопотік Radio 345 працює"
      : paused
        ? "Приймання призупинено, очікуємо відновлення"
        : "Приймач вимкнено або не відповідає";

    updated.textContent = new Date().toLocaleTimeString("uk-UA");
  }

  async function getJSON(path) {
    try {
      return await window.Radio345.request(path);
    } catch (error) {
      if (error.status === 401) expired = true;
      throw error;
    }
  }

  async function refreshStatus() {
    if (busy || expired || document.visibilityState === "hidden") {
      return;
    }

    busy = true;

    try {
      const data = await getJSON("/api/status");
      connection("API підключено", true);
      showReceiver(data.receiver);
    } catch (error) {
      connection("Помилка соединения");
      receiver.textContent = "Немає з’єднання";
      hero.textContent = "● API недоступний";
      description.textContent = error.message;
    } finally {
      busy = false;
    }
  }

  async function start() {
    if (!telegram?.initData) {
      connection("Немає авторизації");
      receiver.textContent = "Відкрий у Telegram";
      description.textContent =
        "Для підключення потрібен запуск через кнопку бота.";
      return;
    }

    connection("Авторизація...");

    try {
      const me = await window.Radio345.user();

      const roles = {
        admin: "Адміністратор",
        editor: "Редактор",
        viewer: "Спостерігач"
      };

      platform.textContent =
        `Telegram · ${roles[me.role] || me.role}`;

      await refreshStatus();

      window.setInterval(refreshStatus, 15000);

      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") {
          refreshStatus();
        }
      });
    } catch (error) {
      connection("Доступ не підтверджено");
      receiver.textContent = "Немає доступу";
      description.textContent = error.message;
    }
  }

  start();
})();
