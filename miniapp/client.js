"use strict";

// Shared transport for management screens. No credentials or phrase text in storage.
window.Radio345 = (() => {
  const base = document.querySelector('meta[name="radio345-api"]').content;
  async function request(path, method = "GET", body, format = "json") {
    const initData = window.Telegram?.WebApp?.initData;
    if (!initData) throw new Error("Відкрий панель через Telegram-бота.");
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
          401: "Авторизація застаріла. Закрий і знову відкрий Mini App.",
          403: "Недостатньо прав для цієї дії.",
          404: "Запис більше недоступний.",
          409: "Дані змінилися. Онови список і повтори виправлення.",
          503: "База тимчасово недоступна. Повтори спробу пізніше."
        };
        let message = messages[response.status] || `Помилка API: HTTP ${response.status}`;
        if (response.status === 422) {
          const error = await response.json().catch(() => ({}));
          message = typeof error.detail === "string" ? error.detail : "Перевір значення полів.";
        }
        const error = new Error(message);
        error.status = response.status;
        throw error;
      }
      return format === "blob" ? await response.blob() : await response.json();
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Сервер не відповів протягом 15 секунд. Повтори спробу.");
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
