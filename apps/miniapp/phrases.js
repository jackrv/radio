"use strict";

// Журнал фраз: пошук, прослуховування й перехід до редактора.
// Словники, навчені заміни, користувачі й налаштування — у локальній панелі на Mac.
(() => {
  const { request, user } = window.Radio345;
  const $ = id => document.getElementById(id);
  const PAGE_SIZE = 10;
  let me = null;
  let offset = 0;
  let query = "";
  let busy = false;
  let returnScroll = null;
  const CONTEXT_KEY = "radio345.phrases.return";
  function rememberContext() {
    try { window.sessionStorage?.setItem(CONTEXT_KEY, JSON.stringify({user: me.user_id, query,
      offset, scroll: window.scrollY || 0, at: Date.now()})); } catch (_) { /* приватний WebView */ }
  }
  function restoreContext() {
    if (new URL(window.location.href).searchParams.has("fix_id")) return;
    try {
      const saved = JSON.parse(window.sessionStorage?.getItem(CONTEXT_KEY) || "null");
      if (!saved || saved.user !== me.user_id || Date.now()-saved.at > 3600000) return;
      query = typeof saved.query === "string" ? saved.query.slice(0, 500) : "";
      offset = Number.isInteger(saved.offset) && saved.offset >= 0 ? saved.offset : 0;
      returnScroll = Math.max(0, Number(saved.scroll) || 0);
      $("phrases-query").value = query;
      window.sessionStorage?.removeItem(CONTEXT_KEY);
    } catch (_) { /* контекст повернення необов'язковий */ }
  }
  const canEdit = () => ["admin", "editor"].includes(me?.role);
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
  function message(text, error = false) {
    $("phrases-feedback").textContent = text;
    $("phrases-feedback").classList.toggle("error-text", error);
  }
  function button(text, action, className = "outline-button") {
    const item = node("button", text, className);
    item.type = "button";
    item.addEventListener("click", action);
    return item;
  }
  function localDate(value) {
    return value ? new Date(value.replace(" ", "T") + "Z").toLocaleString("uk-UA", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"
    }) : "—";
  }

  // Час — як у переліку «Аудіоархіву»: ГГ:ХХ жирно, дата дрібно (лише не сьогодні). Повна дата — у підказці.
  function phraseTime(value) {
    const box = node("span", undefined, "phrase-time");
    if (!value) { box.textContent = "—"; return box; }
    const at = new Date(value.replace(" ", "T") + "Z"), pad = n => String(n).padStart(2, "0");
    box.append(node("b", `${pad(at.getHours())}:${pad(at.getMinutes())}`));
    if (at.toDateString() !== new Date().toDateString()) box.append(node("small", `${pad(at.getDate())}.${pad(at.getMonth() + 1)}`));
    box.title = localDate(value);
    return box;
  }

  function phraseCard(item) {
    const card = node("article", undefined, "data-card");
    const head = node("div", undefined, "card-head");
    head.append(phraseTime(item.created_at));
    const actions = node("div", undefined, "card-actions");
    head.append(actions);
    card.append(head, node("p", item.current_text, "phrase-text"));
    if (item.original_text !== item.current_text) {
      card.append(node("p", `Почуто: ${item.original_text}`, "subtle-note phrase-text"));
    }
    if (item.has_audio) {
      const listen = button("▶", async () => {
        listen.disabled = true;
        try {
          const blob = await request(`/api/phrases/audio?fix_id=${encodeURIComponent(item.fix_id)}`, "GET", undefined, "blob");
          if (!card.isConnected) return;
          const player = node("audio");
          player.controls = true;
          const url = URL.createObjectURL(blob);
          audioUrls.add(url);
          player.src = url;
          player.addEventListener("error", () => {
            message("Пристрій не відтворює OGG. Прослухай голосове в Telegram.", true);
            URL.revokeObjectURL(url); audioUrls.delete(url);
          });
          card.append(player);
          player.play?.().catch?.(() => {});
          listen.hidden = true;
        } catch (error) {
          message(error.message, true);
          listen.disabled = false;
        }
      }, "icon-button");
      listen.setAttribute("aria-label", "Прослухати");
      actions.append(listen);
    }
    if (canEdit()) {
      actions.append(button("Виправити", () => {
        rememberContext();
        const url = new URL(window.location.href);
        url.searchParams.set("fix_id", item.fix_id);
        window.location.assign(url.toString());
      }));
    }
    return card;
  }

  async function load() {
    if (busy || !me) return;
    busy = true;
    $("phrases-prev").disabled = $("phrases-next").disabled = true;
    message("Завантаження…");
    try {
      const data = await request(`/api/phrases?q=${encodeURIComponent(query)}&offset=${offset}&limit=${PAGE_SIZE}`);
      releaseAudio();
      $("phrases-list").replaceChildren();
      for (const item of data.items) $("phrases-list").append(phraseCard(item));
      if (!data.items.length) $("phrases-list").append(node("p", "Фраз немає.", "subtle-note"));
      $("phrases-prev").disabled = offset === 0;
      $("phrases-next").disabled = !data.has_more;
      message(`Сторінка ${offset / PAGE_SIZE + 1}`);
      if (returnScroll !== null) { window.scrollTo?.(0, returnScroll); returnScroll = null; }
    } catch (error) {
      releaseAudio();
      $("phrases-list").replaceChildren();
      message(error.message, true);
      $("phrases-prev").disabled = offset === 0;
      $("phrases-next").disabled = false;
      $("phrases-list").append(button("Повторити завантаження", () => load()));
    } finally { busy = false; }
  }

  $("phrases-search").addEventListener("submit", event => {
    event.preventDefault();
    if (busy) return;
    offset = 0; query = $("phrases-query").value.trim(); load();
  });
  $("phrases-prev").addEventListener("click", () => { if (!busy) { offset = Math.max(0, offset - PAGE_SIZE); load(); } });
  $("phrases-next").addEventListener("click", () => { if (!busy) { offset += PAGE_SIZE; load(); } });

  document.addEventListener("radio345:view", event => {
    if (event.detail === "corrections" && me?.role !== "viewer") load();
  });

  async function start() {
    try {
      me = await user();
      restoreContext();
      if (me.role === "viewer") { showView("home"); return; }
      if (document.querySelector(".view.active")?.id === "view-corrections") await load();
    } catch (error) {
      message(error.message, true);
    }
  }
  start();
})();
