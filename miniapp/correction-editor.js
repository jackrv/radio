"use strict";

(() => {
  const telegram = window.Telegram?.WebApp;
  const fixId = new URLSearchParams(window.location.search).get("fix_id");

  const original = document.getElementById("original-text");
  const edited = document.getElementById("corrected-text");
  const save = document.getElementById("save-correction-button");
  const restore = document.getElementById("restore-example");
  const feedback = document.getElementById("correction-feedback");
  const autoClose = document.getElementById("close-after-save");
  const audioButton = document.getElementById("editor-audio-load");
  const player = document.getElementById("editor-audio");
  const audioFeedback = document.getElementById("editor-audio-feedback");
  let audioUrl = null;

  let draft = null;
  let busy = false;

  // Сохраняем предпочтение отдельно на каждом устройстве.
  const preferenceKey = "radio345:auto-close-after-save";

  try {
    const stored = localStorage.getItem(preferenceKey);
    if (stored !== null) {
      autoClose.checked = stored === "true";
    }
  } catch (_) {
    // Если хранилище недоступно, используем значение чекбокса.
  }

  autoClose.addEventListener("change", () => {
    try {
      localStorage.setItem(
        preferenceKey,
        String(autoClose.checked)
      );
    } catch (_) {
      // Выбор всё равно действует в текущем сеансе.
    }
  });

  async function request(method, body = null) {
    const path = "/api/corrections/draft" + (
      method === "GET" ? `?fix_id=${encodeURIComponent(fixId)}` : ""
    );
    try {
      return await window.Radio345.request(path, method, body === null ? undefined : body);
    } catch (error) {
      if (error.status === 404) {
        throw new Error(fixId?.startsWith("mem:")
          ? "Фраза недоступна: прошло 24 часа или она вытеснена из 200 последних."
          : "Событие не найдено. Возможно, оно удалено.");
      }
      throw error;
    }
  }

  function openEditor() {
    // Keep async journal rendering below the editor. It must not move the field
    // after scrolling, especially in Telegram's mobile WebView.
    const panel = document.getElementById("correction-editor-panel");
    const journal = document.getElementById("phrases-panel");
    if (panel && journal) journal.before(panel);
    panel.hidden = false;
    journal.hidden = true;
    document.querySelector('[data-view-target="corrections"]').click();
  }

  function fitText() {
    if (edited.getClientRects && !edited.getClientRects().length) return;
    edited.style.height = "auto";
    edited.style.height = `${Math.max(80, edited.scrollHeight)}px`;
  }
  edited.addEventListener("input", fitText);
  window.addEventListener("resize", () => { if (draft) fitText(); });
  document.addEventListener("radio345:view", event => {
    if (event.detail === "corrections" && draft) fitText();
  });
  window.addEventListener("pagehide", () => {
    if (audioUrl) URL.revokeObjectURL(audioUrl);
  });
  document.getElementById("editor-back").addEventListener("click", () => {
    const url = new URL(window.location.href);
    url.searchParams.delete("fix_id");
    url.searchParams.set("view", "corrections");
    window.location.assign(url.toString());
  });
  audioButton.addEventListener("click", async () => {
    if (!draft || audioButton.disabled) return;
    audioButton.disabled = true;
    audioFeedback.textContent = "Загружаем аудио из Telegram...";
    try {
      const blob = await window.Radio345.request(`/api/phrases/audio?fix_id=${encodeURIComponent(fixId)}`, "GET", undefined, "blob");
      if (audioUrl) URL.revokeObjectURL(audioUrl);
      audioUrl = URL.createObjectURL(blob);
      player.src = audioUrl;
      player.hidden = false;
      audioButton.hidden = true;
      audioFeedback.textContent = "Нажми ▶ на плеере. Это исходная запись, текстовые правки её не изменяют.";
      fitText();
    } catch (error) {
      audioFeedback.textContent = error.message;
      audioButton.disabled = false;
    }
  });
  player.addEventListener("error", () => {
    audioFeedback.textContent = "Не удалось воспроизвести OGG на этом устройстве. Исходное голосовое доступно в Telegram.";
    if (audioUrl) { URL.revokeObjectURL(audioUrl); audioUrl = null; }
    audioButton.disabled = false;
    audioButton.hidden = false;
    player.hidden = true;
  });

  restore.addEventListener("click", () => {
    if (!draft || busy) return;
    edited.value = draft.current_text;
    fitText();
    edited.focus();
  });

  save.addEventListener("click", async () => {
    if (!draft || busy) return;
    const corrected = edited.value.trim();
    if (!corrected) {
      feedback.textContent = "Текст исправления не может быть пустым.";
      return;
    }
    busy = true;
    save.disabled = true;
    restore.disabled = true;
    feedback.textContent = "Сохраняем исправление...";
    try {
      const result = await request("PUT", {
        fix_id: fixId,
        expected_text: draft.current_text,
        corrected_text: corrected
      });
      draft.current_text = result.corrected_text;
      edited.value = result.corrected_text;
      fitText();
      edited.disabled = true;
      if (
        autoClose.checked &&
        typeof telegram?.close === "function"
      ) {
        feedback.textContent =
          "✅ Исправление сохранено. Закрываем редактор...";

        // Показываем подтверждение перед закрытием.
        window.setTimeout(() => {
          try {
            telegram.close();
          } catch (_) {
            feedback.textContent =
              "✅ Исправление сохранено. Закрой окно вручную.";
          }
        }, 500);
      } else {
        feedback.textContent =
          "✅ Исправление сохранено. Можно закрыть окно или продолжить правку.";
        edited.disabled = false;
        save.disabled = false;
        restore.disabled = false;
      }
    } catch (error) {
      feedback.textContent = error.message;
      save.disabled = false;
      restore.disabled = false;
    } finally {
      busy = false;
    }
  });

  async function start() {
    if (!fixId) return; // Opened through the regular Mini App menu.
    openEditor();
    if (!/^(?:db:[1-9][0-9]{0,17}|mem:[0-9a-f]{32})$/.test(fixId)) {
      feedback.textContent = "Неизвестный идентификатор события.";
      return;
    }
    feedback.textContent = "Загружаем распознанную фразу...";
    try {
      draft = await request("GET");
      original.textContent = draft.original_text;
      edited.value = draft.current_text;
      edited.disabled = false;
      fitText();
      audioButton.disabled = !draft.has_audio;
      audioFeedback.textContent = draft.has_audio
        ? "Прослушай исходное голосовое перед исправлением."
        : "Для этой фразы аудио не привязано. Доступны записи новых голосовых, отправленных после обновления приёмника.";
      save.disabled = false;
      restore.disabled = false;
      feedback.textContent = "Текст уже подставлен. Исправь ошибку и сохрани.";
      // Do not open the mobile keyboard until the operator taps the field.
      // Avoid competing smooth scroll and keyboard viewport animations.
      edited.scrollIntoView({ block: "center", behavior: "instant" });
    } catch (error) {
      feedback.textContent = error.message;
    }
  }

  start();
})();
