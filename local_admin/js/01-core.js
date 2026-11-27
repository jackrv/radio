  const $ = id => document.getElementById(id);
  const REFRESH_MS = 10000;
  const SYSTEM_VIEWS = new Set(["system", "sessions", "storage", "checks"]);   // вкладки «Система»
  let busy = false, data = null, view = "overview", timer = null;
  const loaded = new Set();

  const STATUS = {
    ready: "Розпізнано", ready_fallback: "Розпізнано (резервний текст)",
    short_garbage: "Короткий хибний фрагмент", bare_callsign: "Лише позивний без номера", low_rms: "Тихо (нижче MIN_RMS)", short: "Закоротко (VAD)",
    notification: "Звук сповіщення", blacklist: "Чорний список", garbage: "Сміття Whisper", low_confidence: "Низька впевненість", dictionary_echo: "Перелік назв із підказки",
    empty_text: "Порожній текст", whisper_error: "Помилка Whisper", stale: "Після паузи", discarded: "Відкинуто",
    deferred: "Очікує розпізнавання",
  };
  const SPECTRAL_MODES = { shadow: "Спостереження", active: "Активний", off: "Спектральний вимкнено" };
  const pct = v => (typeof v === "number" ? `${(v * 100).toFixed(1)}%` : "—");
  const yes = v => (v ? "так" : "ні");
  const ROLES = { admin: "Адміністратор", editor: "Редактор", viewer: "Спостерігач" };
  const MODES = { all: "усі фрази", watch: "спостереження", commands: "команди й позивні", off: "вимкнено" };
  // Телефон: рядки таблиць-карток підписують клітинки заголовком колонки (CSS бере його з data-label).
  function labelCells(table) {
    const heads = [...(table.querySelector("tr:has(> th)")?.children || [])].map(th => th.textContent.trim());
    for (const row of table.querySelectorAll("tr")) {
      [...row.children].forEach((cell, i) => { if (cell.tagName === "TD" && heads[i] && !(cell.colSpan > 1)) cell.dataset.label = heads[i]; });
    }
  }
  const cardTables = new MutationObserver(records => {
    for (const table of new Set(records.map(r => r.target.closest?.("table.mobile-cards")).filter(Boolean))) labelCells(table);
  });
  document.addEventListener("DOMContentLoaded", () => {
    for (const t of document.querySelectorAll("table.mobile-cards")) cardTables.observe(t, { childList: true, subtree: true });
  });
  const LABELS = { USE_LLM: "Корекція Qwen (вимкнено 17.11: без покращень, +1,6 с)", VAD_LEVEL: "Рівень VAD (0–3)", SILENCE_END_MS: "Тиша до завершення, мс",
    MIN_SPEECH_MS: "Мінімальне мовлення, мс", MAX_SEGMENT_S: "Максимальний сегмент, с" };
  const LATENCY = { total_latency_s: "До готового тексту", queue_wait_s: "Очікування в черзі", whisper_s: "Whisper",
    qwen_s: "Qwen", speech_tail_s: "Завершення фрази (VAD)", rtf: "RTF (час / тривалість)" };

  // ---------- Допоміжне ----------
  // Повідомлення — спливаючі, на кілька секунд, а не постійний рядок під заголовком.
  let toastTimer = null;
  const note = text => {
    const box = $("feedback");
    box.textContent = text;
    box.classList?.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => box.classList?.remove("show"), Math.min(9000, 3500 + String(text).length * 40));
  };
  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
  function svg(tag, attrs) { const e = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v); return e; }
  const num = v => (typeof v === "number" ? v.toLocaleString("uk-UA") : "—");
  const fix = (v, d = 2) => (typeof v === "number" ? v.toFixed(d) : "—");
  function ago(seconds) {
    if (typeof seconds !== "number" || !isFinite(seconds)) return "—";
    if (seconds < 60) return `${Math.max(0, Math.round(seconds))} с`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} хв`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} год ${Math.round((seconds % 3600) / 60)} хв`;
    return `${Math.floor(seconds / 86400)} д ${Math.floor((seconds % 86400) / 3600)} год`;
  }
  const now = () => Date.now() / 1000;
  const stamp = unix => new Date(unix * 1000).toLocaleString("uk-UA", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  function utcTs(text) { return text ? Date.parse(text.replace(" ", "T") + "Z") / 1000 : null; }

  async function action(callback) { if (busy) return; busy = true; try { await callback(); } catch (error) { note(error.message); } finally { busy = false; } }

