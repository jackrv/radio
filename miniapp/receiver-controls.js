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
      ? "▶ Возобновить"
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
      remaining.textContent = "Состояние загружается...";
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
        `До возобновления: ${minutes}:${rest}`;

      if (actualState === "listening") {
        remaining.textContent += " · Ожидаем остановку микрофона";
      } else if (offline) {
        remaining.textContent += " · Приёмник не запущен";
      }
    } else if (paused) {
      remaining.textContent = "Таймер завершён. Возобновляем приём...";
    } else if (offline) {
      remaining.textContent = "Приёмник не запущен";
    } else {
      remaining.textContent = "Микрофон работает";
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
          "Состояние обновлено. Проверьте его перед новой командой.";
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
        "Сервер не передал версию команды. Обновите API.";
      feedback.style.color = "#ff9b9b";
      return;
    }

    writing = true;
    generation++;
    render();

    feedback.textContent = minutes === 0
      ? "Отправляем команду возобновления..."
      : "Отправляем команду паузы...";
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
        ? "Команда возобновления принята."
        : "Команда паузы принята. Ожидаем микрофон.";

    } catch (error) {
      if (error.conflict) {
        conflictPending = true;

        // Запрещаем повторную команду со старой версией.
        control = null;
        deadline = 0;

        feedback.textContent =
          "Команда отклонена: состояние изменилось " +
          "в другой вкладке. Обновляем данные...";
        feedback.style.color = "#edb07b";
      } else {
        feedback.textContent = `Ошибка: ${error.message}`;
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
      feedback.textContent = "Откройте панель через Telegram.";
      return;
    }

    try {
      const user = await window.Radio345.user();
      isAdmin = user.role === "admin";

      await refresh();

      if (!isAdmin) {
        feedback.textContent =
          "Просмотр доступен. Управление — только администратору.";
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
