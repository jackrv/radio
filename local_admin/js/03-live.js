  // ---------- Огляд: «Ефір зараз» ----------
  const LIVE_TONE = { ready: "ok", ready_fallback: "ok", notification: "info", low_confidence: "warn", blacklist: "warn",
    garbage: "warn", short_garbage: "warn", bare_callsign: "warn", dictionary_echo: "warn", empty_text: "", whisper_error: "bad" };
  async function live() {
    let r;
    try { r = await request("/api/live?limit=30"); } catch (_) { return; }
    const box = $("live");
    if ([...box.querySelectorAll("audio")].some(audio => !audio.paused && !audio.ended)) {
      $("live-note").textContent = "Стрічка оновиться після прослуховування.";
      return;
    }
    $("live-note").textContent = r.skipped_quiet ? `ще ${r.skipped_quiet} тихих чи закоротких не показано` : "";
    if (!r.items.length) { box.replaceChildren(el("p", "muted", "Записів ще немає. Щойно приймач щось почує, фраза з’явиться тут.")); return; }
    box.replaceChildren(...r.items.map(item => {
      const row = el("div", `live-row ${LIVE_TONE[item.status] ?? ""}`);
      // Як рядок переліку архіву: час — ГГ:ХХ великим, секунди дрібно; статус — маленька пігулка.
      const at = new Date(item.created * 1000), pad = n => String(n).padStart(2, "0");
      const time = el("span", "live-time");
      time.append(el("b", "", `${pad(at.getHours())}:${pad(at.getMinutes())}`), el("small", "", `:${pad(at.getSeconds())}`));
      const text = item.final_text || item.rejected_text || item.raw_text;
      const main = el("div", "live-text");
      main.append(time, el("span", text ? "live-phrase" : "live-phrase empty", text || "тексту немає"));
      const meta = el("div", "live-meta");
      meta.append(el("span", `live-status ${LIVE_TONE[item.status] || ""}`, STATUS[item.status] || item.status));
      if (item.fix_id) {
        const sent = el("span", "live-sent", item.fix_id.startsWith("db:") ? `✈ №${item.fix_id.slice(3)}` : "✈");
        sent.title = "Надіслано в Telegram"; meta.append(sent);
      }
      if (item.reference !== null) { const done = el("span", "live-done", "✓"); done.title = "Розмічено"; meta.append(done); }
      const play = el("button", "live-play", "▶"); play.type = "button"; play.title = "Прослухати";
      play.addEventListener("click", () => {
        const audio = el("audio"); audio.controls = true; audio.src = `/api/recordings/${item.id}/audio`;
        play.replaceWith(audio); audio.play().catch(() => {});
      });
      const open = el("button", "live-open", "Розмітити →"); open.type = "button"; open.title = "Відкрити запис в архіві";
      open.addEventListener("click", () => openRecordings({search: item.id.slice(0, 8)}));
      meta.append(play, open);
      row.append(main, meta);
      return row;
    }));
  }

