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
    document.querySelector('[data-view-target="corrections"]').click();
  }

  restore.addEventListener("click", () => {
    if (!draft || busy) return;
    edited.value = draft.current_text;
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
      save.disabled = false;
      restore.disabled = false;
      feedback.textContent = "Текст уже подставлен. Исправь ошибку и сохрани.";
      edited.focus();
      edited.scrollIntoView({ block: "center", behavior: "smooth" });
    } catch (error) {
      feedback.textContent = error.message;
    }
  }

  start();
})();
