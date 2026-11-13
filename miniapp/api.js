"use strict";

(() => {
  const API = "https://643d-5-248-51-168.ngrok-free.app";
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

    receiver.textContent = listening
      ? "Слушает эфир"
      : "Приём остановлен";

    hero.textContent = listening
      ? "● Приём активен"
      : "● Нет активного приёма";

    hero.style.background = listening ? "#36564e" : "#33404e";

    description.textContent = listening
      ? "Аудиопоток Radio 345 работает"
      : "Приёмник отключён или не отвечает";

    updated.textContent = new Date().toLocaleTimeString("ru-RU");
  }

  async function getJSON(path) {
    const response = await fetch(`${API}${path}`, {
      method: "GET",
      headers: {
        Authorization: `tma ${telegram.initData}`,
        "ngrok-skip-browser-warning": "true"
      },
      credentials: "omit",
      cache: "no-store",
      referrerPolicy: "no-referrer"
    });

    if (response.status === 401) {
      expired = true;
      throw new Error("Авторизация истекла. Открой панель заново.");
    }

    if (response.status === 403) {
      throw new Error("Нет доступа к Radio 345.");
    }

    if (!response.ok) {
      throw new Error(`Ошибка API: HTTP ${response.status}`);
    }

    return response.json();
  }

  async function refreshStatus() {
    if (busy || expired || document.visibilityState === "hidden") {
      return;
    }

    busy = true;

    try {
      const data = await getJSON("/api/status");
      connection("API подключён", true);
      showReceiver(data.receiver);
    } catch (error) {
      connection("Ошибка соединения");
      receiver.textContent = "Нет соединения";
      hero.textContent = "● API недоступен";
      description.textContent = error.message;
    } finally {
      busy = false;
    }
  }

  async function start() {
    if (!telegram?.initData) {
      connection("Нет авторизации");
      receiver.textContent = "Открой в Telegram";
      description.textContent =
        "Для подключения нужен запуск через кнопку бота.";
      return;
    }

    connection("Авторизация...");

    try {
      const me = await getJSON("/api/me");

      const roles = {
        admin: "Администратор",
        editor: "Редактор",
        viewer: "Наблюдатель"
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
      connection("Доступ не подтверждён");
      receiver.textContent = "Нет доступа";
      description.textContent = error.message;
    }
  }

  start();
})();
