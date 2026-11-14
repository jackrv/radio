"use strict";

// Shared transport for management screens. No credentials or phrase text in storage.
window.Radio345 = (() => {
  const base = document.querySelector('meta[name="radio345-api"]').content;
  async function request(path, method = "GET", body) {
    const initData = window.Telegram?.WebApp?.initData;
    if (!initData) throw new Error("Открой панель через Telegram-бота.");
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(base + path, {
        method,
        headers: {
          Authorization: `tma ${initData}`,
          "ngrok-skip-browser-warning": "true",
          ...(body === undefined ? {} : { "Content-Type": "application/json" })
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer"
      });
      if (!response.ok) {
        const messages = {
          401: "Авторизация истекла. Закрой и заново открой Mini App.",
          403: "Недостаточно прав для этого действия.",
          404: "Запись больше недоступна.",
          409: "Данные изменились. Обнови список и повтори правку.",
          503: "База временно недоступна. Повтори попытку позже."
        };
        let message = messages[response.status] || `Ошибка API: HTTP ${response.status}`;
        if (response.status === 422) {
          const error = await response.json().catch(() => ({}));
          message = typeof error.detail === "string" ? error.detail : "Проверь значения полей.";
        }
        const error = new Error(message);
        error.status = response.status;
        throw error;
      }
      return await response.json();
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Сервер не ответил за 15 секунд. Повтори попытку.");
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }
  let me;
  function user() {
    if (!me) me = request("/api/me").catch(error => { me = null; throw error; });
    return me;
  }
  return { request, user };
})();
