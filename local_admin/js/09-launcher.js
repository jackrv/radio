  // ---------- Лаунчер: перезапуск і зупинка ----------
  const LAUNCHER = { running: "працює", starting: "запускається", stopping: "зупиняється", cleanup_pending: "завершує служби", inactive: "не керує панеллю" };
  async function launcherState() {
    try {
      const s = await request("/api/launcher");
      $("launcher-state").textContent = s.managed ? (LAUNCHER[s.state] || s.state) : LAUNCHER.inactive;
      $("launcher-restart").disabled = $("launcher-stop").disabled = $("receiver-toggle").disabled = !s.managed || !s.reachable;
      receiverStopped = !!s.receiver_stopped;
      if (s.managed && receiverStopped) $("launcher-state").textContent = "приймач зупинено";
      $("receiver-toggle").innerHTML = receiverStopped
        ? '<svg class="ico solid" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l11-6.5z"/></svg>'
        : '<svg class="ico solid" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
      $("receiver-toggle").setAttribute("aria-label", receiverStopped ? "Запустити приймач" : "Зупинити лише приймач");
      $("receiver-toggle").title = receiverStopped ? "Запустити приймач" : "Зупинити лише приймач (панель, API й Mini App працюють далі)";
    } catch (_) { $("launcher-state").textContent = "—"; }
  }
  let receiverStopped = false;
  $("receiver-toggle").addEventListener("click", () => action(async () => {
    const start = receiverStopped;
    if (!start && !confirm("Зупинити лише приймач? Ефір не записуватиметься й сповіщень не буде, доки його не запустять знову. Панель, API й Mini App працюють.")) return;
    await request(`/api/launcher/${start ? "receiver-start" : "receiver-stop"}`, "POST");
    note(start ? "Приймач запускається (близько 10 с)…" : "Приймач зупиняється: дочікується черги Telegram…");
    setTimeout(launcherState, 8000);
  }));
  async function waitForComeback() {
    // Панель перезапускається разом з усіма службами; сесія зберігається.
    for (let i = 0; i < 90; i++) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      try { const s = await request("/api/launcher"); if (s.managed && s.state === "running" && i > 2) { location.reload(); return; } } catch (_) {}
    }
    note("Панель не повернулась за 3 хв. Перевірте вікно лаунчера або launcher.log.");
  }
  $("launcher-restart").addEventListener("click", () => action(async () => {
    if (!confirm("Перезапустити всі служби? Приймач, бот і Mini App будуть недоступні близько хвилини. Черга Telegram буде дочекана.")) return;
    await request("/api/launcher/restart", "POST");
    $("launcher-state").textContent = "перезапускається";
    note("Перезапуск… Сторінка оновиться сама, коли панель повернеться.");
    waitForComeback();
  }));
  $("launcher-stop").addEventListener("click", () => action(async () => {
    if (!confirm("Зупинити Radio 345 повністю? Сповіщення не надходитимуть, ця панель теж зупиниться. Запустити знову можна лише на Mac: у папці проєкту командою .venv/bin/python start_radio345.py")) return;
    await request("/api/launcher/stop", "POST");
    $("launcher-state").textContent = "зупиняється";
    note("Лаунчер зупиняється. Запустити знову: .venv/bin/python start_radio345.py у папці проєкту.");
  }));
  launcherState();
  setInterval(launcherState, 30000);

