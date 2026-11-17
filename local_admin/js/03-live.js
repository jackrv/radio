  // ---------- Огляд: «Ефір зараз» ----------
  const LIVE_TONE = { ready: "ok", ready_fallback: "ok", notification: "info", low_confidence: "warn", blacklist: "warn",
    garbage: "warn", short_garbage: "warn", bare_callsign: "warn", dictionary_echo: "warn", empty_text: "", whisper_error: "bad" };
  async function live() {
    let r;
    try { r = await request("/api/live?limit=15"); } catch (_) { return; }
    const box = $("live");
    if ([...box.querySelectorAll("audio")].some(audio => !audio.paused && !audio.ended)) {
      $("live-note").textContent = "Стрічка оновиться після прослуховування.";
      return;
    }
    $("live-note").textContent = r.skipped_quiet ? `ще ${r.skipped_quiet} тихих чи закоротких не показано` : "";
    if (!r.items.length) { box.replaceChildren(el("p", "muted", "Записів ще немає. Щойно приймач щось почує, фраза з’явиться тут.")); return; }
    box.replaceChildren(...r.items.map(item => {
      const row = el("div", `live-row ${LIVE_TONE[item.status] ?? ""}`);
      const time = new Date(item.created * 1000).toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      const text = item.final_text || item.rejected_text || item.raw_text;
      const main = el("div", "live-text");
      main.append(el("span", "live-time", time), el("span", "", text || "— тексту немає —"));
      const meta = el("div", "live-meta");
      meta.append(el("span", `badge ${LIVE_TONE[item.status] || ""}`, STATUS[item.status] || item.status));
      if (item.fix_id) meta.append(el("span", "badge", item.fix_id.startsWith("db:") ? `Telegram · №${item.fix_id.slice(3)}` : "Telegram"));
      if (item.reference !== null) meta.append(el("span", "badge", "✓ розмічено"));
      const play = el("button", "ghost small-button", "▶"); play.type = "button"; play.title = "Прослухати";
      play.addEventListener("click", () => {
        const audio = el("audio"); audio.controls = true; audio.src = `/api/recordings/${item.id}/audio`;
        play.replaceWith(audio); audio.play().catch(() => {});
      });
      const open = el("button", "ghost small-button", "Розмітити"); open.type = "button";
      open.addEventListener("click", () => openRecordings({search: item.id.slice(0, 8)}));
      meta.append(play, open);
      row.append(main, meta);
      return row;
    }));
  }

