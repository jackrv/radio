"use strict";

// Navigation. API-based correction editing is in correction-editor.js.
const availableViews = new Set(["home", "corrections", "dictionaries", "settings", "users"]);

function showView(name) {
  if (!availableViews.has(name)) return;
  for (const section of document.querySelectorAll(".view")) {
    const active = section.id === `view-${name}`;
    section.hidden = !active;
    section.classList.toggle("active", active);
  }
  for (const button of document.querySelectorAll(".nav-link")) {
    const active = button.dataset.viewTarget === name;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  window.scrollTo({ top: 0, behavior: "instant" });
  document.dispatchEvent(new CustomEvent("radio345:view", { detail: name }));
}

for (const button of document.querySelectorAll("[data-view-target]")) {
  button.addEventListener("click", () => showView(button.dataset.viewTarget));
}

const webApp = window.Telegram?.WebApp;
if (webApp) {
  try {
    webApp.ready();
    webApp.expand();
  } catch (_) {
    // Локальный предпросмотр работает и без клиента Telegram.
  }
}

document.getElementById("platform-label").textContent =
  webApp?.initData ? "Telegram Mini App · API ожидается" : "Локальный предпросмотр · без авторизации";
