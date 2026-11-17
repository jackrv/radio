  // ---------- Аналіз: якість по днях ----------
  async function quality() {
    const r = await request("/api/quality?days=60");
    const table = $("quality-days"), head = el("tr");
    table.replaceChildren();
    for (const h of ["День", "Розмічено", "Без правок", "Пропущено", "Помилка в словах: Whisper", "після обробки", "Номери точно", "Адресат вірно"]) head.append(el("th", "", h));
    table.append(head);
    if (!r.days.length) {
      const row = el("tr"), cell = el("td", "muted", "Розмічених записів мовлення за цей період немає. Розмітьте кілька в «Аудіоархіві».");
      cell.colSpan = 8; row.append(cell); table.append(row); return;
    }
    const bar = (value, good) => {
      const cell = el("td", "num");
      if (value == null) { cell.textContent = "—"; return cell; }
      const wrap = el("div", "bar-cell"), fill = el("i", good ? "good" : "bad");
      fill.style.width = `${Math.round(Math.min(1, value) * 100)}%`;
      wrap.append(fill, el("span", "", pct(value)));
      cell.append(wrap);
      return cell;
    };
    // Підсумок за весь період — зважений за кількістю записів і слів.
    const sum = key => r.days.reduce((a, d) => a + (d[key] || 0), 0);
    const share = (a, b) => (b ? a / b : null);
    const total = { day: "Усього", records: sum("records"), no_edits_share: share(sum("no_edits"), sum("records")),
      missed_share: share(sum("missed"), sum("records")), wer_raw: share(sum("raw_errors"), sum("words")),
      wer_final: share(sum("final_errors"), sum("words")), numbers: share(sum("numbers_ok"), sum("with_numbers")),
      recipient: share(sum("roles_ok"), sum("roles")) };
    for (const d of [total, ...[...r.days].reverse()]) {
      const row = el("tr", d === total ? "total" : "");
      row.append(el("td", "nowrap", d === total ? "Усього" : d.day.split("-").reverse().join(".")), el("td", "num", String(d.records)),
        bar(d.no_edits_share, true), bar(d.missed_share, false), bar(d.wer_raw, false), bar(d.wer_final, false),
        bar(d.numbers, true), bar(d.recipient, true));
      table.append(row);
    }
  }

  // ---------- Аналіз: офлайн-прогони ----------
  const EXP_KEYS = [["wer", "Помилка в словах", false], ["digits_exact", "Номери точно", true], ["recipient_accuracy_from_hypothesis", "Адресат", true],
    ["missed_speech", "Пропущено мовлення", false, true], ["false_text_on_non_speech", "Текст на шумі/сигналі", false, true],
    ["false_text_on_room_noise", "Текст на кімнаті", false, true], ["latency_p90_s", "Затримка p90, с", false, true]];
  const expValue = (key, value, count) => value == null ? "—" : count ? (key.includes("latency") ? fix(value, 1) : num(value)) : pct(value);
  const picked = new Set();
  async function experiments() {
    const r = await request("/api/experiments");
    const table = $("experiments-list"), head = el("tr");
    table.replaceChildren(); picked.clear(); $("experiments-compare").disabled = true; $("experiments-result").replaceChildren();
    for (const h of ["", "Коли", "Назва", "Що перевірялось", "Записів", ...EXP_KEYS.map(k => k[1])]) head.append(el("th", "", h));
    table.append(head);
    if (!r.items.length) { const row = el("tr"), cell = el("td", "muted", "Прогонів ще немає."); cell.colSpan = 12; row.append(cell); table.append(row); return; }
    for (const run of r.items) {
      const row = el("tr"), pick = el("input"), p = run.params || {};
      pick.type = "checkbox";
      pick.addEventListener("change", () => {
        if (pick.checked) picked.add(run.name); else picked.delete(run.name);
        $("experiments-compare").disabled = picked.size !== 2;
      });
      const started = p.started_utc ? new Date(p.started_utc.replace(/^(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)Z$/, "$1-$2-$3T$4:$5:$6Z")) : null;
      const what = [p.variant, p.prompt && `підказка: ${p.prompt}`, p.model && String(p.model).replace(/^mlx-community\//, ""), p.llm && `Qwen: ${p.llm}`].filter(Boolean).join(" · ");
      const cells = [el("td"), el("td", "nowrap", started ? started.toLocaleString("uk-UA", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—"),
        el("td", "", run.label), el("td", "small muted", what), el("td", "num", run.complete ? num(run.done) : `${num(run.done)} / ${num(p.records || 0)} · не завершено`)];
      cells[0].append(pick);
      for (const [key, , , count] of EXP_KEYS) cells.push(el("td", "num", expValue(key, run.summary?.[key], count)));
      row.append(...cells); table.append(row);
    }
  }
  $("experiments-compare").addEventListener("click", () => action(async () => {
    const [a, b] = [...picked];
    const r = await request(`/api/experiments/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
    const table = el("table", "compact"), head = el("tr");
    for (const h of ["Показник", a.split("_").slice(1).join("_"), b.split("_").slice(1).join("_")]) head.append(el("th", "", h));
    table.append(head);
    for (const [key, title, higherBetter, count] of EXP_KEYS) {
      const va = r.a[key], vb = r.b[key], row = el("tr");
      const better = va == null || vb == null || va === vb ? null : (higherBetter ? vb > va : vb < va) ? "b" : "a";
      row.append(el("td", "", title), el("td", `num ${better === "a" ? "good" : ""}`, expValue(key, va, count)), el("td", `num ${better === "b" ? "good" : ""}`, expValue(key, vb, count)));
      table.append(row);
    }
    const box = $("experiments-result");
    box.replaceChildren(el("p", "small", `Порівняння на ${num(r.common)} спільних записах. Зеленим — кращий результат.`), table);
  }));

