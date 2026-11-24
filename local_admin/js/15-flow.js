  // ---------- Система → Схема: що працює й куди зараз ідуть дані ----------
  const FLOW_PULSE_MS = 4000;   // разова подія (фраза, сповіщення) світиться стільки
  const FLOW_NODES = ["radio", "node", "asr", "db", "whisper", "telegram", "panel", "iphone"];
  const flowUntil = {};
  let flowTimer = null, flowBusy = false, flowLast = null, flowHome = null, flowDevices = null, flowDevicesAt = 0;

  // Чиста функція: знімки → стан вузлів [тон, підпис] і зв’язків (true — дані йдуть).
  function flowModel(flow, devicesState, until, at) {
    const live = flow?.data && flow.age_s < 6 ? flow.data : null, home = flow?.home || devicesState?.marker || null;
    const run = (device, process) => devicesState?.[device]?.processes?.[process]?.alive;
    const process = name => home ? run(home, name) : undefined;
    const service = (name, on) => { const alive = process(name); return alive === undefined ? ["", "немає даних"] : alive ? ["ok", on] : ["bad", "зупинено"]; };
    const hot = key => (until[key] || 0) > at;
    const air = live?.air === true, tx = !!live?.transmitting, listeners = live?.listeners || 0, queue = live?.queue || 0;
    const worker = run("mac", "worker"), model = devicesState?.mac?.model;
    const modelState = model?.state || model;   // рядок або {state}
    // Обмін із Whisper відмічає сама служба розпізнавання; за чергою не вгадуємо.
    const w = live?.whisper, sending = w?.state === "sending" && w.age_s < 75 && process("asr") !== false;
    const asking = !!live && (sending || hot("whisper"));
    const whisper = sending ? ["busy", "розпізнає"]
      : worker === false ? ["bad", "зупинено"]
      : modelState === "paused" ? ["warn", "віддано лабораторії"]
      : modelState === "waiting" ? ["warn", "чекає модель"]
      : w?.state === "unavailable" ? ["bad", `Mac не відповів ${ago(w.age_s)} тому`]
      : w?.state === "refused" ? ["warn", `зайнятий ${ago(w.age_s)} тому`]
      : worker ? ["ok", "готовий, на Mac"]
      : w?.state === "ready" ? [w.age_s < 600 ? "ok" : "", `відповів ${ago(w.age_s)} тому`]
      : ["", "на Mac, ще не питали"];
    const nodeState = !live?.node ? service("node", "працює")
      : live.node === "paused" ? ["warn", "пауза"] : live.node === "listening" ? ["ok", "слухає"] : ["bad", "зупинено"];
    const nodes = {
      radio: tx ? ["busy", "передає"] : air ? ["busy", "приймає"] : live?.node === "listening" && live.air === false ? ["ok", "тиша"] : ["", "немає даних"],
      node: nodeState[0] === "ok" && (air || tx) ? ["busy", tx ? "передача" : "мовлення"] : nodeState,
      asr: service("asr", queue ? `у черзі ${queue}` : "чекає фразу"),
      db: hot("db") ? ["busy", "запис"] : live ? ["ok", "й архів"] : ["", "немає даних"],
      whisper,
      telegram: service("asr", process("tunnel") ? "бот, Mini App" : "бот"),
      panel: ["ok", "ви тут"],
      iphone: tx ? ["busy", "передає"] : listeners ? ["ok", `слухачів: ${listeners}`] : live?.listeners === undefined ? ["", "немає даних"] : ["", "не підключено"],
    };
    if (sending && nodes.asr[0] === "ok") nodes.asr = ["busy", queue > 1 ? `у черзі ${queue}` : "чекає текст"];
    if (hot("telegram") && nodes.telegram[0] === "ok") nodes.telegram = ["busy", "надсилання"];
    return { live: !!live, home, nodes, edges: {
      air, tx, segment: hot("segment"), whisper: asking, db: hot("db"), telegram: hot("telegram"), panel: hot("db"),
      listen: listeners > 0 && air, talk: tx } };
  }
  // Рівень входу: шкала −60…−10 dBFS, колір за порогом MIN_RMS.
  function flowLevel(live) {
    const level = live?.level, rms = level?.rms, min = live?.min_rms;
    const db = v => 20 * Math.log10(Math.max(v, 1e-5)), part = v => Math.min(1, Math.max(0, (db(v) + 60) / 50));
    if (typeof rms !== "number") return { tone: "off", now: "рівня немає", phrase: "", passed: "", part: 0, mark: 0 };
    const over = typeof min === "number" && rms >= min;
    // Один рядок, коли остання фраза сама пройшла поріг; інакше — вона й остання, що пройшла.
    const last = level.phrase_rms, known = typeof last === "number", line = (value, age) => `${value.toFixed(4)} · ${ago(age)}`;
    const passed = typeof min === "number" ? (level.louder || []).findLast(p => p[0] >= min) : null;
    const quiet = known && typeof min === "number" && last < min;
    return { tone: level.peak >= 0.985 ? "clip" : over ? "above" : "", part: part(rms), mark: typeof min === "number" ? part(min) : 0,
      now: `RMS ${rms.toFixed(4)} · ${Math.round(db(rms))} дБ`,
      phrase: quiet ? `тихо ${line(last, level.phrase_age_s)}` : "",
      passed: passed ? `✓ ${line(...passed)}` : typeof min === "number" ? `поріг ${min}` : known ? line(last, level.phrase_age_s) : "" };
  }
  // Що змінилося між двома знімками. Перший знімок — лише точка відліку.
  function flowChanges(before, after) {
    if (!before || !after) return [];
    const grew = key => typeof before[key] === "number" && typeof after[key] === "number" && after[key] > before[key];
    const found = [];
    if (after.segment && after.segment !== before.segment && (!before.segment || after.segment > before.segment)) found.push("segment");
    if (grew("event") || after.queue < before.queue) found.push("db");
    // Короткий обмін може вміститися між знімками.
    if (before.whisper && after.whisper && after.whisper.sent > before.whisper.sent) found.push("whisper");
    if (grew("sent")) found.push("telegram");
    return found;
  }
  function flowRender(flow) {
    const at = Date.now();
    const m = flowModel(flow, flowDevices, flowUntil, at);
    for (const name of FLOW_NODES) {
      const [tone, text] = m.nodes[name], box = $(`flow-${name}`);
      box.setAttribute("class", `flow-node ${tone}`);
      box.querySelector(".flow-sub").textContent = text;
    }
    const level = flowLevel(m.live ? flow.data : null), meter = $("flow-level");
    meter.setAttribute("class", `flow-level ${level.tone}`);
    meter.querySelector(".flow-level-now").textContent = level.now;
    meter.querySelector(".flow-level-phrase").textContent = level.phrase;
    meter.querySelector(".flow-level-passed").textContent = level.passed;
    $("flow-level-bar").setAttribute("width", String(Math.round(130 * level.part)));
    $("flow-level-mark").setAttribute("transform", `translate(${20 + Math.round(130 * level.mark)} 0)`);
    for (const [name, on] of Object.entries(m.edges)) $(`flow-edge-${name}`).classList.toggle("on", on);
    $("flow-home-title").textContent = m.home ? `Дім: ${deviceTitle(m.home)}` : "Дім: невідомо";
    const status = m.live ? "Наживо" : flow?.home === null ? "Дім на іншому пристрої: живих даних тут немає" : "Чекаю дані з дому…";
    if ($("flow-status").textContent !== status) $("flow-status").textContent = status;
    $("flow-status").classList.toggle("good", m.live);
  }
  async function flowView() {
    clearTimeout(flowTimer);
    if (flowBusy) return;
    flowBusy = true;
    let flow = null;
    try {
      // Стан служб повільний (Pi через SSH) — не затримує рух даних.
      if (Date.now() - flowDevicesAt > 10000) {
        flowDevicesAt = Date.now();
        request("/api/devices").then(d => { flowDevices = d; }, () => { flowDevices = null; });
      }
      flow = await request("/api/flow");
      if (flow.home !== flowHome) { flowHome = flow.home; flowLast = null; }
      if (flow.data) {
        for (const key of flowChanges(flowLast, flow.data)) flowUntil[key] = Date.now() + FLOW_PULSE_MS;
        flowLast = flow.data;
      }
    } catch (_) { /* без відповіді схема гасне; про зв’язок каже шапка */ }
    finally { flowBusy = false; }
    flowRender(flow);
    if (view === "flow") flowTimer = setTimeout(flowView, document.visibilityState === "visible" ? 1000 : 5000);
  }
