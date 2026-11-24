  // ---------- Служби: r345 (перезапуск і зупинка) або systemd на Pi ----------
  const SERVICE_MODE = { r345: "r345", systemd: "systemd (Pi)", none: "запущено окремо" };
  let serviceMode = "none";
  async function launcherState() {
    try {
      const s = await request("/api/launcher");
      serviceMode = s.mode;
      $("launcher-state").textContent = s.stopped?.length ? `не працює: ${s.stopped.join(", ")}` : SERVICE_MODE[s.mode] || s.mode;
      const own = s.mode === "r345";
      $("launcher-restart").disabled = $("launcher-stop").disabled = !own;
      $("launcher-hint").textContent = own ? "Зупинка закриє й цю панель; запустити знову: ./r345 up."
        : s.mode === "systemd" ? "Службами керує systemd: sudo systemctl restart radio345-…" : "Панель запущено окремо, без r345.";
    } catch (_) { $("launcher-state").textContent = "—"; }
  }
  async function waitForComeback() {
    // Панель перезапускається разом з усіма службами; сесія зберігається.
    for (let i = 0; i < 90; i++) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      try { const s = await request("/api/launcher"); if (s.mode === "r345" && i > 2) { location.reload(); return; } } catch (_) {}
    }
    note("Панель не повернулась за 3 хв. Перевірте вікно r345.");
  }
  $("launcher-restart").addEventListener("click", () => action(async () => {
    if (!confirm("Перезапустити всі служби? Вони будуть недоступні близько хвилини.")) return;
    await request("/api/launcher/restart", "POST");
    $("launcher-state").textContent = "перезапускається";
    note("Перезапуск… Сторінка оновиться сама, коли панель повернеться.");
    waitForComeback();
  }));
  $("launcher-stop").addEventListener("click", () => action(async () => {
    if (!confirm("Зупинити всі служби? Ця панель теж зупиниться. Запустити знову: ./r345 up")) return;
    await request("/api/launcher/stop", "POST");
    $("launcher-state").textContent = "зупиняється";
    note("Служби зупиняються. Запустити знову: ./r345 up у папці проєкту.");
  }));
  launcherState();
  setInterval(launcherState, 30000);
