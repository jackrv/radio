  // ---------- Огляд: «Ефір зараз» ----------
  const LIVE_TONE = { ready: "ok", ready_fallback: "ok", notification: "info", low_confidence: "warn", blacklist: "warn",
    garbage: "warn", short_garbage: "warn", bare_callsign: "warn", dictionary_echo: "warn", empty_text: "", whisper_error: "bad" };
  const LIVE_PHRASES_KEY = "radio345:live-phrases";
  try { $("live-phrases").checked = localStorage.getItem(LIVE_PHRASES_KEY) === "on"; } catch (_) { /* типово — усе */ }
  $("live-phrases").addEventListener("change", () => {
    try { localStorage.setItem(LIVE_PHRASES_KEY, $("live-phrases").checked ? "on" : "off"); } catch (_) { /* лише сесія */ }
    live();
  });
  async function live() {
    let r;
    // «Лише фрази» — без «Кхм», коротких хибних фрагментів тощо; тоді беремо більше записів, щоб було що показати.
    const phrasesOnly = $("live-phrases").checked;
    try { r = await request(`/api/live?limit=${phrasesOnly ? 50 : 30}`); } catch (_) { return; }
    if (phrasesOnly) r = { ...r, items: r.items.filter(i => ["ready", "ready_fallback"].includes(i.status) || i.fix_id || i.reference !== null) };
    const box = $("live");
    if ([...box.querySelectorAll("audio")].some(audio => !audio.paused && !audio.ended)) {
      $("live-note").textContent = "Стрічка оновиться після прослуховування.";
      return;
    }
    $("live-note").textContent = r.skipped_quiet ? `ще ${r.skipped_quiet} тихих чи закоротких не показано` : "";
    if (!r.items.length) { box.replaceChildren(el("p", "muted", phrasesOnly ? "Серед останніх записів фраз немає — зніміть «лише фрази», щоб побачити все."
                                                                              : "Записів ще немає. Щойно приймач щось почує, фраза з’явиться тут.")); return; }
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
      // Номер повідомлення Telegram — першим і в колонці сталої ширини (порожньою, якщо не надсилалось),
      // щоб пігулки статусу стояли рівно одна під одною.
      const sent = el("span", "live-sent", item.fix_id ? (item.fix_id.startsWith("db:") ? `✈ №${item.fix_id.slice(3)}` : "✈") : "");
      if (item.fix_id) sent.title = "Надіслано в Telegram";
      meta.append(sent, el("span", `live-status ${LIVE_TONE[item.status] || ""}`, STATUS[item.status] || item.status));
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

