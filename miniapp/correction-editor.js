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
  const learningBox = document.getElementById("correction-learning");
  const checkBox = document.getElementById("correction-check");
  let audioUrl = null;

  // ---------- Що з правки потрапило в навчання ----------
  const CANDIDATE_STATES = {
    waiting: c => `${c.confirmations} з ${c.required} підтверджень — потрібні правки інших фраз`,
    needs_approval: () => "підтверджено; застосовується після дозволу в «Словники → Правила»",
    active: () => "активне правило",
    conflict: () => "є інший варіант виправлення — не застосовується, доки конфлікт не зникне",
    disabled: () => "правило вимкнено",
  };
  const KINDS = { callsign: "позивний, лише перед дозволеним номером", word: "звичайне слово" };
  const DECISIONS = { pending: "очікує рішення", confirmed: "навчати", rejected: "не навчати" };

  function el(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  }

  function renderLearning(report) {
    learningBox.replaceChildren();
    learningBox.hidden = !report;
    if (!report) return;
    if (report.format_only) {
      learningBox.append(el("p", "Змінено лише регістр, пунктуацію або запис числа — навчання не потрібне.", "subtle-note"));
    }
    const candidates = report.candidates || [];
    learningBox.append(el("p", `Кандидатів для навчання: ${candidates.length}`, "learning-title"));
    const list = el("ul", undefined, "learning-list");
    for (const c of candidates) {
      const row = el("li");
      row.append(el("strong", `${c.original} → ${c.corrected}`),
        el("span", ` · ${KINDS[c.kind] || c.kind} · ${(CANDIDATE_STATES[c.state] || (() => c.state))(c)}`));
      list.append(row);
    }
    if (candidates.length) learningBox.append(list);

    const review = report.review || [];
    if (review.length) {
      learningBox.append(el("p", "Потребує перевірки (без рішення не навчає):", "learning-title"));
      const box = el("ul", undefined, "learning-list");
      for (const r of review) {
        const row = el("li");
        row.append(el("strong", `${r.original} → ${r.corrected}`), el("span", ` · ${r.message} · ${DECISIONS[r.state]}`));
        const actions = el("div", undefined, "learning-actions");
        const choices = r.state === "pending"
          ? [["Навчати", "confirmed"], ["Не навчати", "rejected"]]
          : [["Скасувати рішення", "pending"]];
        for (const [label, decision] of choices) {
          const choice = el("button", label, "outline-button");
          choice.type = "button";
          choice.addEventListener("click", () => decide(r, decision, choice));
          actions.append(choice);
        }
        row.append(actions);
        box.append(row);
      }
      learningBox.append(box);
    }

    const excluded = report.excluded || [];
    if (excluded.length) {
      const details = el("details", undefined, "learning-excluded");
      details.append(el("summary", `Не навчається: ${excluded.length}`));
      const box = el("ul", undefined, "learning-list");
      for (const e of excluded) {
        const row = el("li");
        row.append(el("strong", e.label), el("span", ` · ${e.message}`));
        box.append(row);
      }
      details.append(box);
      learningBox.append(details);
    }
  }

  async function decide(item, decision, control) {
    if (!draft || busy) return;
    busy = true;
    control.disabled = true;
    try {
      const report = await window.Radio345.request("/api/corrections/learning", "PUT", {
        fix_id: fixId, expected_text: draft.current_text,
        original: item.original, corrected: item.corrected, decision,
      });
      renderLearning(report);
      feedback.textContent = decision === "confirmed" ? "✅ Пару додано до навчання."
        : decision === "rejected" ? "Пару не буде використано для навчання." : "Рішення скасовано.";
    } catch (error) {
      feedback.textContent = error.message;
      control.disabled = false;
    } finally {
      busy = false;
    }
  }

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
          ? "Фраза недоступна: минуло 24 години або її витіснено з 200 останніх."
          : "Подію не знайдено. Можливо, її видалено.");
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

  // ---------- Перевірка оформлення: лише підказки, зберегти можна будь-що ----------
  let checkTimer = null;
  let checkSeq = 0;
  function useText(text) {
    edited.value = text;
    fitText();
    checkText(0);
  }
  function renderCheck(result) {
    checkBox.replaceChildren();
    const issues = result?.issues || [];
    if (!issues.length) return;
    if (result.formatted) {
      const all = el("button", "Виправити оформлення", "outline-button");
      all.type = "button";
      all.addEventListener("click", () => useText(result.formatted));
      checkBox.append(all);
    }
    for (const issue of issues) {
      const row = el("div", undefined, `text-check-item ${issue.kind}`);
      row.append(el("span", issue.message));
      if (issue.fixed_text) {
        const fix = el("button", "Виправити", "outline-button");
        fix.type = "button";
        fix.addEventListener("click", () => useText(issue.fixed_text));
        row.append(fix);
      }
      checkBox.append(row);
    }
  }
  function checkText(delay = 600) {
    window.clearTimeout?.(checkTimer);
    checkTimer = window.setTimeout(async () => {
      const text = edited.value;
      const seq = ++checkSeq;
      if (!text.trim()) { renderCheck(null); return; }
      try {
        const result = await window.Radio345.request("/api/corrections/check", "PUT", { text });
        if (seq === checkSeq && edited.value === text) renderCheck(result);
      } catch (_) {
        // Перевірка необов'язкова (наприклад, API ще не оновлено) — редактор працює як раніше.
        renderCheck(null);
      }
    }, delay);
  }
  edited.addEventListener("input", () => checkText());
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
    audioFeedback.textContent = "Завантажуємо аудіо з Telegram...";
    try {
      const blob = await window.Radio345.request(`/api/phrases/audio?fix_id=${encodeURIComponent(fixId)}`, "GET", undefined, "blob");
      if (audioUrl) URL.revokeObjectURL(audioUrl);
      audioUrl = URL.createObjectURL(blob);
      player.src = audioUrl;
      player.hidden = false;
      audioButton.hidden = true;
      audioFeedback.textContent = "Натисни ▶ на плеєрі. Це початковий запис, текстові зміни його не змінюють.";
      fitText();
    } catch (error) {
      audioFeedback.textContent = error.message;
      audioButton.disabled = false;
    }
  });
  player.addEventListener("error", () => {
    audioFeedback.textContent = "Не вдалося відтворити OGG на цьому пристрої. Початкове голосове доступне в Telegram.";
    if (audioUrl) { URL.revokeObjectURL(audioUrl); audioUrl = null; }
    audioButton.disabled = false;
    audioButton.hidden = false;
    player.hidden = true;
  });

  restore.addEventListener("click", () => {
    if (!draft || busy) return;
    useText(draft.current_text);
    edited.focus();
  });

  save.addEventListener("click", async () => {
    if (!draft || busy) return;
    const corrected = edited.value.trim();
    if (!corrected) {
      feedback.textContent = "Текст виправлення не може бути порожнім.";
      return;
    }
    busy = true;
    save.disabled = true;
    restore.disabled = true;
    feedback.textContent = "Зберігаємо виправлення...";
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
      renderLearning(result.learning);
      // Неоднозначні пари чекають рішення оператора: вікно не закриваємо.
      const needsDecision = (result.learning?.review || []).some(r => r.state === "pending");
      const savedNote = result.text_changed ? "✅ Фразу збережено." : "✅ Збережено, текст не змінено.";
      if (
        autoClose.checked && !needsDecision &&
        typeof telegram?.close === "function"
      ) {
        feedback.textContent =
          "✅ Виправлення збережено. Закриваємо редактор...";

        // Показываем подтверждение перед закрытием.
        window.setTimeout(() => {
          try {
            telegram.close();
          } catch (_) {
            feedback.textContent =
              "✅ Виправлення збережено. Закрий вікно вручну.";
          }
        }, 500);
      } else {
        feedback.textContent = needsDecision
          ? `${savedNote} Перевір пари нижче: без рішення вони не навчають систему.`
          : `${savedNote} Можна закрити вікно або продовжити виправлення.`;
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
      feedback.textContent = "Невідомий ідентифікатор події.";
      return;
    }
    feedback.textContent = "Завантажуємо розпізнану фразу...";
    try {
      draft = await request("GET");
      original.textContent = draft.original_text;
      edited.value = draft.current_text;
      renderLearning(draft.learning);
      edited.disabled = false;
      fitText();
      audioButton.disabled = !draft.has_audio;
      audioFeedback.textContent = draft.has_audio
        ? "Прослухай початкове голосове перед виправленням."
        : "Для цієї фрази аудіо не прив’язано. Доступні записи нових голосових, надісланих після оновлення приймача.";
      save.disabled = false;
      restore.disabled = false;
      checkText(0);
      feedback.textContent = "Текст уже підставлено. Виправ помилку та збережи.";
      // Do not open the mobile keyboard until the operator taps the field.
      // Avoid competing smooth scroll and keyboard viewport animations.
      edited.scrollIntoView({ block: "center", behavior: "instant" });
    } catch (error) {
      feedback.textContent = error.message;
    }
  }

  start();
})();
