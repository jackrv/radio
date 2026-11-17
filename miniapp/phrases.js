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

  function phraseCard(item) {
    const card = node("article", undefined, "data-card");
    const head = node("div", undefined, "card-head");
    head.append(node("span", localDate(item.created_at), "subtle-note"));
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
    } catch (error) {
      $("phrases-list").replaceChildren();
      message(error.message, true);
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
      if (me.role === "viewer") { showView("home"); return; }
      if (document.querySelector(".view.active")?.id === "view-corrections") await load();
    } catch (error) {
      message(error.message, true);
    }
  }
  start();
})();
