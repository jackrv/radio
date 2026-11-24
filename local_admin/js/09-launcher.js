  // ---------- Служби: r345 (перезапуск і зупинка) або systemd на Pi ----------
  const SERVICE_MODE = { r345: "r345", systemd: "systemd (Pi)", none: "запущено окремо" };
  let serviceMode = "none";
  // Замість спливних вікон: перше натискання питає прямо в кнопці, друге — виконує; за 4 с питання зникає.
  function armed(button, question, run) {
    let timer = null, label = null;
    const reset = () => { clearTimeout(timer); timer = null; if (label !== null) button.replaceChildren(...label); label = null; button.classList.remove("armed"); };
    button.addEventListener("click", () => {
      if (timer === null) {
        label = [...button.childNodes]; button.replaceChildren(question); button.classList.add("armed");
        timer = setTimeout(reset, 4000); return;
      }
      reset(); action(run);
    });
  }
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
  armed($("launcher-restart"), "Перезапустити? Ще раз", async () => {
    await request("/api/launcher/restart", "POST");
    $("launcher-state").textContent = "перезапускається";
    note("Перезапуск… Сторінка оновиться сама, коли панель повернеться.");
    waitForComeback();
  });
  armed($("launcher-stop"), "Зупинити й цю панель? Ще раз", async () => {
    await request("/api/launcher/stop", "POST");
    $("launcher-state").textContent = "зупиняється";
    note("Служби зупиняються. Запустити знову: ./r345 up у папці проєкту.");
  });
  launcherState();
  setInterval(launcherState, 30000);
