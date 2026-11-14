"use strict";

(() => {

  const telegram = window.Telegram?.WebApp;
  const button = document.getElementById("receiver-pause-button");
  const select = document.getElementById("receiver-pause-minutes");
  const remaining = document.getElementById("receiver-pause-remaining");
  const feedback = document.getElementById("receiver-pause-feedback");

  let isAdmin = false;
  let control = null;
  let actualState = "offline";
  let deadline = 0;
  let writing = false;
  let reading = false;
  let generation = 0;
  let conflictPending = false;

  async function request(method, path, payload) {
    try {
      return await window.Radio345.request(path, method, payload);
    } catch (error) {
      error.conflict = error.status === 409;
      throw error;
    }
  }

  function setControl(data) {
    control = data;
    deadline = performance.now() +
      Math.max(0, data.remaining_seconds || 0) * 1000;
    render();
  }

  function render() {
    const requested = control?.pause_requested === true;
    const offline = actualState === "offline";
    const paused = actualState === "paused";

    button.textContent = requested
      ? "▶ Відновити"
      : "⏸ Пауза";

    button.disabled = (
      !isAdmin ||
      writing ||
      !control ||
      (offline && !requested) ||
      (paused && !requested)
    );

    select.disabled = !isAdmin || writing || requested || offline;

    if (!control) {
      remaining.textContent = "Стан завантажується...";
      return;
    }

    if (requested) {
      const seconds = Math.max(
        0,
        Math.ceil((deadline - performance.now()) / 1000)
      );

      const minutes = Math.floor(seconds / 60);
      const rest = String(seconds % 60).padStart(2, "0");

      remaining.textContent =
        `До відновлення: ${minutes}:${rest}`;

      if (actualState === "listening") {
        remaining.textContent += " · Очікуємо зупинки мікрофона";
      } else if (offline) {
        remaining.textContent += " · Приймач не запущено";
      }
    } else if (paused) {
      remaining.textContent = "Таймер завершено. Відновлюємо приймання...";
    } else if (offline) {
      remaining.textContent = "Приймач не запущено";
    } else {
      remaining.textContent = "Мікрофон працює";
    }
  }

  async function refresh() {
    if (
      writing || reading ||
      document.visibilityState === "hidden" ||
      !telegram?.initData
    ) {
      return;
    }

    reading = true;
    const expectedGeneration = generation;

    try {
      const [command, status] = await Promise.all([
        request("GET", "/api/receiver/control"),
        request("GET", "/api/status")
      ]);

      if (expectedGeneration !== generation || writing) {
        return;
      }

      actualState = status.receiver;
      setControl(command);

      if (conflictPending) {
        conflictPending = false;
        feedback.textContent =
          "Стан оновлено. Перевірте його перед новою командою.";
        feedback.style.color = "#edb07b";
      }
    } catch (error) {
      feedback.textContent = error.message;
      feedback.style.color = "#ff9b9b";
    } finally {
      reading = false;
    }
  }

  async function updatePause() {
    if (!isAdmin || writing || !control) return;

    const minutes = control.pause_requested
      ? 0
      : Number(select.value);

    const expectedRevision = control.revision;

    if (!Number.isInteger(minutes)) return;

    if (
      !Number.isSafeInteger(expectedRevision) ||
      expectedRevision < 0
    ) {
      feedback.textContent =
        "Сервер не передав версію команди. Оновіть API.";
      feedback.style.color = "#ff9b9b";
      return;
    }

    writing = true;
    generation++;
    render();

    feedback.textContent = minutes === 0
      ? "Надсилаємо команду відновлення..."
      : "Надсилаємо команду паузи...";
    feedback.style.color = "#a3c0b2";

    try {
      const result = await request(
        "PUT",
        "/api/receiver/control",
        {
          minutes,
          expected_revision: expectedRevision
        }
      );

      setControl(result);

      feedback.textContent = minutes === 0
        ? "Команду відновлення прийнято."
        : "Команду паузи прийнято. Очікуємо мікрофон.";

    } catch (error) {
      if (error.conflict) {
        conflictPending = true;

        // Запрещаем повторную команду со старой версией.
        control = null;
        deadline = 0;

        feedback.textContent =
          "Команду відхилено: стан змінився " +
          "в іншій вкладці. Оновлюємо дані...";
        feedback.style.color = "#edb07b";
      } else {
        feedback.textContent = `Помилка: ${error.message}`;
        feedback.style.color = "#ff9b9b";
      }
    } finally {
      writing = false;
      render();
      await refresh();
    }
  }

  button.addEventListener("click", updatePause);

  async function start() {
    if (!telegram?.initData) {
      feedback.textContent = "Відкрийте панель через Telegram.";
      return;
    }

    try {
      const user = await window.Radio345.user();
      isAdmin = user.role === "admin";

      await refresh();

      if (!isAdmin) {
        feedback.textContent =
          "Перегляд доступний. Керування — лише адміністратору.";
      }

      window.setInterval(render, 1000);
      window.setInterval(refresh, 3000);

      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") {
          refresh();
        }
      });
    } catch (error) {
      feedback.textContent = error.message;
      feedback.style.color = "#ff9b9b";
    }
  }

  start();
})();
