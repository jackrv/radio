const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const root = path.join(__dirname, "..", "miniapp");
const source = name => fs.readFileSync(path.join(root, name), "utf8");
const tick = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.events = {};
    this.value = "";
    this.textContent = "";
    this.style = {};
    this.isConnected = true;
    this.scrollHeight = 100;
    const classes = new Set();
    this.classList = {
      add: value => classes.add(value), remove: value => classes.delete(value),
      contains: value => classes.has(value),
      toggle: (value, active) => active ? classes.add(value) : classes.delete(value)
    };
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute() {}
  focus() { this.focused = true; }
  scrollIntoView(options) { this.scrolled = options; }
  before(item) { this.beforeItem = item; }
  click() {}
  addEventListener(name, callback) { (this.events[name] ||= []).push(callback); }
  async fire(name) { for (const callback of this.events[name] || []) await callback({ preventDefault() {} }); await tick(); }
}

function documentFixture() {
  const elements = new Map([...source("index.html").matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
  const doc = new Element();
  doc.getElementById = id => {
    assert(elements.has(id), `Missing HTML element ${id}`);
    return elements.get(id);
  };
  doc.createElement = tag => new Element(tag);
  doc.querySelector = selector => selector.includes("meta") ? { content: "https://api.example" }
    : selector === ".view.active" ? { id: "view-home" } : new Element();
  doc.dispatchEvent = event => { for (const callback of doc.events[event.type] || []) callback(event); };
  return { doc, get: doc.getElementById };
}

function clientFixture(fetch, hasAuth = true, timers = {}) {
  const { doc } = documentFixture();
  const context = {
    document: doc, fetch, AbortController, Error,
    window: { Telegram: hasAuth ? { WebApp: { initData: "signed-data" } } : null,
      setTimeout: timers.setTimeout || setTimeout, clearTimeout: timers.clearTimeout || clearTimeout }
  };
  vm.runInNewContext(source("client.js"), context);
  return context.window.Radio345;
}

test("client sends signed auth, JSON, no cache or credentials", async () => {
  let captured;
  const client = clientFixture(async (url, options) => {
    captured = { url, options };
    return { ok: true, json: async () => ({ saved: true }) };
  });
  await client.request("/api/users/123", "PUT", { enabled: false });
  assert.equal(captured.url, "https://api.example/api/users/123");
  assert.equal(captured.options.headers.Authorization, "tma signed-data");
  assert.equal(captured.options.cache, "no-store");
  assert.equal(captured.options.credentials, "omit");
  assert.equal(captured.options.referrerPolicy, "no-referrer");
  assert.deepEqual(JSON.parse(captured.options.body), { enabled: false });
});

test("client never sends a request without Telegram authorization", async () => {
  let calls = 0;
  const client = clientFixture(async () => { calls++; }, false);
  await assert.rejects(client.request("/api/phrases"), /Telegram/);
  assert.equal(calls, 0);
});

test("audio is fetched as a blob with the same authentication", async () => {
  const audio = new Blob(["OggSfake"]);
  const client = clientFixture(async () => ({ ok: true, blob: async () => audio }));
  assert.equal(await client.request("/api/phrases/audio?fix_id=db%3A1", "GET", undefined, "blob"), audio);
});

for (const code of [401, 403, 404, 409, 503]) {
  test(`client preserves HTTP ${code} for screen error handling`, async () => {
    const client = clientFixture(async () => ({ ok: false, status: code }));
    await assert.rejects(client.request("/api/phrases"), error => error.status === code);
  });
}

test("client aborts a stalled request and clears timer", async () => {
  let timeout;
  let cleared = false;
  const client = clientFixture((url, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener("abort", () => { const e = new Error(); e.name = "AbortError"; reject(e); });
  }), true, { setTimeout: callback => { timeout = callback; return 10; }, clearTimeout: () => { cleared = true; } });
  const request = client.request("/api/overview");
  timeout();
  await assert.rejects(request, /15 секунд/);
  assert.equal(cleared, true);
});

test("concurrent screens share the verified user request", async () => {
  let calls = 0;
  const client = clientFixture(async () => { calls++; return { ok: true, json: async () => ({ role: "admin" }) }; });
  await Promise.all([client.user(), client.user(), client.user()]);
  assert.equal(calls, 1);
});

async function managementFixture(role = "admin", override) {
  const { doc, get } = documentFixture();
  get("dictionary-kind").value = "callsigns";
  const calls = [];
  const request = async (url, method = "GET", body) => {
    calls.push({ url, method, body });
    if (override) { const result = override(url, method, body); if (result !== undefined) return result; }
    if (url === "/api/overview") return { events: 2, active_rules: 1, dictionary_entries: 3 };
    if (url.startsWith("/api/phrases")) return { items: [{ fix_id: "db:1", current_text: "<img src=x onerror=alert(1)>", created_at: "2026-11-14 12:00:00" }], has_more: false };
    if (url.startsWith("/api/dictionaries/")) return { kind: "callsigns", version: "a".repeat(64), entries: body?.entries || [{ raw_line: "Ліра: 68", enabled: true }] };
    if (url.startsWith("/api/word-rules")) return { items: [], has_more: false };
    if (url === "/api/users") return { items: [{ user_id: "2002", role: "viewer", enabled: true, version: "a".repeat(64) }] };
    if (url.startsWith("/api/users/")) return { saved: true };
    if (url === "/api/recognition/settings") return { revision: 1, values: { USE_LLM: true, VAD_LEVEL: 2, SILENCE_END_MS: 600, MIN_SPEECH_MS: 500, MAX_SEGMENT_S: 20 } };
    throw new Error("Unexpected request " + url);
  };
  const win = new Element();
  Object.assign(win, { Radio345: { request, user: async () => ({ role, user_id: 1001 }) }, confirm: () => true,
    location: { href: "https://panel.example/", assign() {} } });
  vm.runInNewContext(source("management.js"), { document: doc, window: win, URL, Date, console });
  await tick();
  const view = async name => { doc.dispatchEvent({ type: "radio345:view", detail: name }); await tick(); };
  return { get, calls, view };
}

test("viewer can browse phrases but receives no edit controls", async () => {
  const { get, view, calls } = await managementFixture("viewer");
  await view("corrections");
  const card = get("phrases-list").children[0];
  assert.equal(card.children[1].textContent, "<img src=x onerror=alert(1)>");
  assert.equal(card.children.some(item => item.tagName === "button"), false);
  await view("users");
  await view("settings");
  assert.equal(calls.some(c => ["/api/users", "/api/recognition/settings"].includes(c.url)), false);
});

test("editor can save dictionary text and enabled state with snapshot version", async () => {
  const { get, view, calls } = await managementFixture("editor");
  await view("dictionaries");
  const row = get("dictionary-entries").children[0];
  row.children[1].value = "Ліра: 68 69";
  await row.children[1].fire("input");
  await get("dictionary-save").fire("click");
  const call = calls.find(c => c.method === "PUT");
  assert.equal(call.body.expected_version, "a".repeat(64));
  assert.equal(call.body.entries[0].raw_line, "Ліра: 68 69");
  assert.match(get("dictionary-feedback").textContent, /Перезапусти/);
});

test("dictionary conflict keeps the operator's unsaved input", async () => {
  const { get, view } = await managementFixture("editor", (url, method) => {
    if (method === "PUT") { const e = new Error("Конфликт"); e.status = 409; throw e; }
  });
  await view("dictionaries");
  const input = get("dictionary-entries").children[0].children[1];
  input.value = "Вовк"; await input.fire("input");
  await get("dictionary-save").fire("click");
  assert.equal(get("dictionary-entries").children[0].children[1].value, "Вовк");
  assert.match(get("dictionary-feedback").textContent, /Конфликт/);
});

test("viewer dictionary buttons and fields stay disabled", async () => {
  const { get, view, calls } = await managementFixture("viewer");
  await view("dictionaries");
  assert.equal(get("dictionary-save").disabled, true);
  assert.equal(get("dictionary-entries").children[0].children[1].disabled, true);
  await get("dictionary-save").fire("click");
  assert.equal(calls.some(c => c.method === "PUT"), false);
});

test("viewer can load an authenticated audio player beside a phrase", async () => {
  const { get, view, calls } = await managementFixture("viewer", (url) => {
    if (url.startsWith("/api/phrases?")) return { items: [{ fix_id: "db:1", current_text: "Ліра", has_audio: true }], has_more: false };
    if (url.startsWith("/api/phrases/audio?")) return new Blob(["OggSfake"]);
  });
  await view("corrections");
  const card = get("phrases-list").children[0];
  const listen = card.children.find(child => child.tagName === "button");
  await listen.fire("click");
  assert.equal(listen.hidden, true);
  assert.equal(card.children.at(-1).tagName, "audio");
  assert.equal(card.children.at(-1).controls, true);
  assert(calls.some(call => call.url === "/api/phrases/audio?fix_id=db%3A1"));
});

test("admin user edit sends expected version and requires confirmation", async () => {
  const { get, view, calls } = await managementFixture();
  await view("users");
  const row = get("users-list").children[0].children[1];
  row.children[0].value = "editor";
  await row.children[2].fire("click");
  const call = calls.find(c => c.method === "PUT");
  assert.equal(call.url, "/api/users/2002");
  assert.equal(call.body.role, "editor");
  assert.equal(call.body.expected_version, "a".repeat(64));
});

test("settings form sends typed values and expected revision", async () => {
  const { get, view, calls } = await managementFixture();
  await view("settings");
  get("setting-llm").checked = false;
  get("setting-vad").value = "3";
  await get("recognition-form").fire("submit");
  const call = calls.find(c => c.method === "PUT");
  assert.equal(call.url, "/api/recognition/settings");
  assert.equal(call.body.values.USE_LLM, false);
  assert.equal(call.body.values.VAD_LEVEL, 3);
  assert.equal(call.body.expected_revision, 1);
  assert.match(get("recognition-feedback").textContent, /Перезапусти/);
});

async function correctionFixture(autoClose, hasAudio = false) {
  const { doc, get } = documentFixture();
  get("close-after-save").checked = autoClose;
  const writes = [];
  let closed = false;
  const window = {
    Telegram: { WebApp: { close: () => { closed = true; } } },
    location: { search: "?fix_id=mem:" + "a".repeat(32) },
    setTimeout: callback => callback(),
    addEventListener() {},
    Radio345: { request: async (url, method, body) => {
      if (url.startsWith("/api/phrases/audio")) return new Blob(["OggSfake"]);
      if (method === "GET") return { original_text: "Лира 68", current_text: "Лира 68", has_audio: hasAudio };
      writes.push(body);
      return { saved: true, corrected_text: body.corrected_text };
    } }
  };
  vm.runInNewContext(source("correction-editor.js"), {
    window, document: doc, URLSearchParams, URL,
    localStorage: { getItem: () => null, setItem() {} }
  });
  await tick();
  return { get, writes, isClosed: () => closed };
}

test("editor can save twice without auto-close using the latest expected text", async () => {
  const { get, writes, isClosed } = await correctionFixture(false);
  assert.equal(get("corrected-text").value, "Лира 68");
  assert.equal(get("phrases-panel").beforeItem, get("correction-editor-panel"));
  assert.equal(get("corrected-text").focused, undefined);
  assert.equal(get("corrected-text").scrolled.behavior, "instant");
  get("corrected-text").value = "Ліра 68";
  await get("save-correction-button").fire("click");
  assert.equal(get("corrected-text").disabled, false);
  get("corrected-text").value = "Ліро 68";
  await get("save-correction-button").fire("click");
  assert.equal(writes.length, 2);
  assert.equal(writes[1].expected_text, "Ліра 68");
  assert.equal(isClosed(), false);
});

test("adding a dictionary row focuses and reveals the new final row", async () => {
  const { get, view } = await managementFixture("editor");
  await view("dictionaries");
  await get("dictionary-add").fire("click");
  const row = get("dictionary-entries").children.at(-1);
  assert.equal(row.children[1].value, "");
  assert.equal(row.children[1].focused, true);
  assert.equal(row.children[1].scrolled.block, "center");
  assert.equal(row.classList.contains("dictionary-row-new"), true);
  assert.match(get("dictionary-feedback").textContent, /Новая строка добавлена/);
});

function keyboardFixture(nativeMethod) {
  const { doc, get } = documentFixture();
  doc.documentElement = new Element();
  const viewport = new Element();
  viewport.height = 400;
  viewport.offsetTop = 10;
  const window = new Element();
  Object.assign(window, { innerHeight: 800, visualViewport: viewport,
    requestAnimationFrame: callback => callback(), Telegram: { WebApp: { hideKeyboard: nativeMethod } } });
  const field = new Element("input");
  field.value = "Несохранённая строка";
  field.matches = () => true;
  field.blur = () => { doc.activeElement = null; field.blurred = true; };
  doc.activeElement = field;
  vm.runInNewContext(source("keyboard.js"), { window, document: doc });
  doc.dispatchEvent({ type: "focusin", target: field });
  return { doc, field, viewport, done: get("keyboard-done") };
}

test("keyboard button uses the visible viewport and native Telegram dismissal", async () => {
  let hidden = 0;
  const { done, field, doc, viewport } = keyboardFixture(() => hidden++);
  assert.equal(done.hidden, false);
  assert.equal(done.style.top, "354px");
  viewport.height = 300;
  await viewport.fire("resize");
  assert.equal(done.style.top, "254px");
  await done.fire("click");
  assert.equal(hidden, 1);
  assert.equal(field.blurred, true);
  assert.equal(field.value, "Несохранённая строка");
  assert.equal(done.hidden, true);
  assert.equal(doc.documentElement.classList.contains("text-entry-active"), false);
});

test("keyboard dismissal works with older Telegram clients without hideKeyboard", async () => {
  const { done, field } = keyboardFixture(undefined);
  await done.fire("click");
  assert.equal(field.blurred, true);
  assert.equal(done.hidden, true);
});

test("a rejected native hideKeyboard call still blurs the field", async () => {
  const { done, field } = keyboardFixture(() => { throw new Error("unsupported"); });
  await done.fire("click");
  assert.equal(field.blurred, true);
  assert.equal(done.hidden, true);
});

test("background taps dismiss keyboard while taps on controls do not", async () => {
  const { doc, field } = keyboardFixture();
  doc.dispatchEvent({ type: "pointerdown", target: { closest: () => ({}) } });
  assert.equal(field.blurred, undefined);
  doc.dispatchEvent({ type: "pointerdown", target: { closest: () => null } });
  assert.equal(field.blurred, true);
});

test("editor still closes Telegram after a successful save when requested", async () => {
  const { get, isClosed } = await correctionFixture(true);
  get("corrected-text").value = "Ліра 68";
  await get("save-correction-button").fire("click");
  assert.equal(isClosed(), true);
});

test("editor has its own player and preserves it after text is corrected", async () => {
  const { get } = await correctionFixture(false, true);
  assert.equal(get("phrases-panel").hidden, true);
  assert.equal(get("correction-editor-panel").hidden, false);
  assert.equal(get("editor-audio-load").disabled, false);
  await get("editor-audio-load").fire("click");
  assert.equal(get("editor-audio").hidden, false);
  assert.match(get("editor-audio").src, /^blob:/);
  const src = get("editor-audio").src;
  get("corrected-text").value = "Ліра 68";
  await get("save-correction-button").fire("click");
  assert.equal(get("editor-audio").src, src);
  assert.equal(get("corrected-text").style.height, "100px");
  URL.revokeObjectURL(src);
});

test("editor explains when an old phrase has no linked audio", async () => {
  const { get } = await correctionFixture(false, false);
  assert.equal(get("editor-audio-load").disabled, true);
  assert.match(get("editor-audio-feedback").textContent, /не привязано/);
});

test("admin can assign a friendly user name", async () => {
  const { get, view, calls } = await managementFixture();
  await view("users");
  const card = get("users-list").children[0];
  card.children.at(-1).value = "Оператор смены";
  await card.children[1].children[2].fire("click");
  assert.equal(calls.find(call => call.method === "PUT").body.display_name, "Оператор смены");
});
