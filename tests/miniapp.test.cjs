const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const root = path.join(__dirname, "..", "apps", "miniapp");
const source = name => fs.readFileSync(path.join(root, name), "utf8");
const tick = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
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
  get options() { return this.children; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute() {}
  focus() { this.focused = true; }
  scrollIntoView(options) { this.scrolled = options; }
  before(item) { this.beforeItem = item; }
  click() {}
  addEventListener(name, callback) { (this.events[name] ||= []).push(callback); }
  async fire(name, event = {}) {
    for (const callback of this.events[name] || []) await callback({ preventDefault() {}, ...event });
    await tick();
  }
}

function documentFixture() {
  const elements = new Map([...source("index.html").matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
  const doc = new Element();
  doc.body = new Element("body");
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

test("client sends signed auth, JSON, no cache or cross-origin cookies", async () => {
  let captured;
  const client = clientFixture(async (url, options) => {
    captured = { url, options };
    return { ok: true, json: async () => ({ saved: true }) };
  });
  await client.request("/api/users/123", "PUT", { enabled: false });
  assert.equal(captured.url, "https://api.example/api/users/123");
  assert.equal(captured.options.headers.Authorization, "tma signed-data");
  assert.equal(captured.options.cache, "no-store");
  assert.equal(captured.options.credentials, "same-origin");
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

async function phrasesFixture(role = "admin", override, options = {}) {
  const { doc, get } = documentFixture();
  const calls = [];
  const request = async (url, method = "GET", body) => {
    calls.push({ url, method, body });
    if (override) { const result = override(url, method, body); if (result !== undefined) return result; }
    if (url.startsWith("/api/phrases")) return { items: [{ fix_id: "db:1", current_text: "<img src=x onerror=alert(1)>", original_text: "<img src=x onerror=alert(1)>", created_at: "2026-11-14 12:00:00" }], has_more: false };
    throw new Error("Unexpected request " + url);
  };
  const win = new Element();
  Object.assign(win, { Radio345: { request, user: async () => ({ role, user_id: options.userId || 1001 }) },
    sessionStorage: options.storage, scrollY: 123, scrollTo(x,y) { win.restoredScroll=y; },
    location: { href: "https://panel.example/", assign(url) { win.assigned = url; } } });
  vm.runInNewContext(source("phrases.js"), { document: doc, window: win, URL, Date, console, showView() {} });
  await tick();
  const view = async name => { doc.dispatchEvent({ type: "radio345:view", detail: name }); await tick(); };
  return { get, calls, view, win };
}

test("Mini App has only home and corrections; management lives in the local panel", () => {
  const html = source("index.html");
  const tabs = [...html.matchAll(/data-view-target="([^"]+)"/g)].map(match => match[1]);
  assert.deepEqual(tabs, ["home", "corrections"]);
  for (const id of ["dictionary-kind", "users-list", "recognition-form", "rules-list", "events-count"]) {
    assert.doesNotMatch(html, new RegExp(`id="${id}"`), `${id} перенесено в локальну панель`);
  }
  assert.equal(fs.existsSync(path.join(root, "management.js")), false);
});

test("viewer never loads phrases", async () => {
  const { get, view, calls } = await phrasesFixture("viewer");
  for (const name of ["corrections", "home"]) await view(name);
  assert.equal(get("phrases-list").children.length, 0);
  assert.equal(calls.length, 0);
});

test("journal renders phrase text as text and opens the editor", async () => {
  const { get, view, win } = await phrasesFixture("editor");
  await view("corrections");
  const card = get("phrases-list").children[0];
  assert.equal(card.children[1].textContent, "<img src=x onerror=alert(1)>");
  const edit = card.children[0].children[1].children.find(child => child.textContent === "Виправити");
  await edit.fire("click");
  assert.match(win.assigned, /fix_id=db%3A1/);
});

test("editor can load an authenticated audio player beside a phrase", async () => {
  const { get, view, calls } = await phrasesFixture("editor", (url) => {
    if (url.startsWith("/api/phrases?")) return { items: [{ fix_id: "db:1", current_text: "Віра", original_text: "Віра", has_audio: true }], has_more: false };
    if (url.startsWith("/api/phrases/audio?")) return new Blob(["OggSfake"]);
  });
  await view("corrections");
  const card = get("phrases-list").children[0];
  const listen = card.children[0].children[1].children[0];
  await listen.fire("click");
  assert.equal(listen.hidden, true);
  assert.equal(card.children.at(-1).tagName, "audio");
  assert.equal(card.children.at(-1).controls, true);
  assert(calls.some(call => call.url === "/api/phrases/audio?fix_id=db%3A1"));
});

test('journal recovers explicitly after a failed page load', async () => {
  let fail=true;
  const h=await phrasesFixture('editor',url=>{
    if (url.startsWith('/api/phrases?') && fail) {fail=false;throw Error('offline');}
  });
  await h.view('corrections');
  assert.match(h.get('phrases-feedback').textContent,/offline/);
  assert.equal(h.get('phrases-next').disabled,false);
  const retry=h.get('phrases-list').children.find(n=>n.textContent==='Повторити завантаження');
  assert(retry);await retry.fire('click');
  assert.match(h.get('phrases-feedback').textContent,/Сторінка 1/);
});

test('return context keeps search and page in session storage for the same user', async () => {
  const values=new Map();const storage={setItem:(k,v)=>values.set(k,v),getItem:k=>values.get(k),removeItem:k=>values.delete(k)};
  const first=await phrasesFixture('editor',url=>url.startsWith('/api/phrases?')
    ? {items:[{fix_id:'db:1',current_text:'Клен',original_text:'Клен'}],has_more:true}:undefined,{storage});
  await first.view('corrections');first.get('phrases-query').value='Клен 12';
  await first.get('phrases-search').fire('submit');await first.get('phrases-next').fire('click');
  const card=first.get('phrases-list').children[0];
  await card.children[0].children[1].children.find(n=>n.textContent==='Виправити').fire('click');
  const other=await phrasesFixture('editor',undefined,{storage,userId:2002});await other.view('corrections');
  assert(!other.calls.some(c=>c.url.includes('offset=10')));
  const back=await phrasesFixture('editor',undefined,{storage});await back.view('corrections');
  assert(back.calls.some(c=>c.url.includes('q=%D0%9A%D0%BB%D0%B5%D0%BD%2012') && c.url.includes('offset=10')));
  assert.equal(back.win.restoredScroll,123);
});

async function correctionFixture(autoClose, hasAudio = false, check = () => ({ issues: [], formatted: null })) {
  const { doc, get } = documentFixture();
  get("close-after-save").checked = autoClose;
  const writes = [];
  const checks = [];
  let closed = false;
  const window = {
    Telegram: { WebApp: { close: () => { closed = true; } } },
    location: { search: "?fix_id=mem:" + "a".repeat(32) },
    setTimeout: callback => callback(),
    addEventListener() {},
    Radio345: { request: async (url, method, body) => {
      if (url.startsWith("/api/phrases/audio")) return new Blob(["OggSfake"]);
      if (url === "/api/corrections/check") {
        // CORS сервера (server/api/app.py) дозволяє лише GET і PUT: інакше браузер заблокує запит.
        assert.ok(["GET", "PUT"].includes(method), `метод ${method} заборонено CORS`);
        checks.push(body.text); return check(body.text);
      }
      if (method === "GET") return { original_text: "Вира 68", current_text: "Вира 68", has_audio: hasAudio };
      writes.push(body);
      return { saved: true, corrected_text: body.corrected_text };
    } }
  };
  vm.runInNewContext(source("correction-editor.js"), {
    window, document: doc, URLSearchParams, URL,
    localStorage: { getItem: () => null, setItem() {} }
  });
  await tick();
  return { get, writes, checks, isClosed: () => closed };
}

test("editor shows text check hints and applies a fix only on request", async () => {
  const hints = text => text === "Вира 68, с півночі"
    ? { formatted: "Вира 68, з півночі", issues: [{ kind: "format", message: "Українською — «з»", fixed_text: "Вира 68, з півночі" }] }
    : { issues: [], formatted: null };
  const { get, writes, checks } = await correctionFixture(false, false, hints);
  assert.deepEqual(checks, ["Вира 68"], "перевірка одразу після завантаження");
  get("corrected-text").value = "Вира 68, с півночі";
  await get("corrected-text").fire("input");
  const box = get("correction-check");
  assert.equal(box.children[0].textContent, "Виправити оформлення");
  const row = box.children[1];
  assert.equal(row.children[0].textContent, "Українською — «з»");
  assert.equal(get("corrected-text").value, "Вира 68, с півночі", "без натискання текст не змінюється");
  await row.children[1].fire("click");
  assert.equal(get("corrected-text").value, "Вира 68, з півночі");
  assert.equal(box.children.length, 0, "після виправлення зауважень немає");
  assert.equal(writes.length, 0, "перевірка нічого не зберігає");
});

test("editor works when the API has no text check yet", async () => {
  const { get, writes } = await correctionFixture(false, false, () => { throw new Error("404"); });
  get("corrected-text").value = "Віра 68";
  await get("corrected-text").fire("input");
  assert.equal(get("correction-check").children.length, 0);
  await get("save-correction-button").fire("click");
  assert.equal(writes.length, 1);
});

test("editor can save twice without auto-close using the latest expected text", async () => {
  const { get, writes, isClosed } = await correctionFixture(false);
  assert.equal(get("corrected-text").value, "Вира 68");
  assert.equal(get("phrases-panel").beforeItem, get("correction-editor-panel"));
  assert.equal(get("corrected-text").focused, undefined);
  assert.equal(get("corrected-text").scrolled.behavior, "instant");
  get("corrected-text").value = "Віра 68";
  await get("save-correction-button").fire("click");
  assert.equal(get("corrected-text").disabled, false);
  get("corrected-text").value = "Ліно 68";
  await get("save-correction-button").fire("click");
  assert.equal(writes.length, 2);
  assert.equal(writes[1].expected_text, "Віра 68");
  assert.equal(isClosed(), false);
});

function keyboardFixture(nativeMethod) {
  const { doc } = documentFixture();
  doc.documentElement = new Element();
  const window = new Element();
  Object.assign(window, { requestAnimationFrame: callback => callback(), Telegram: { WebApp: { hideKeyboard: nativeMethod } } });
  const field = new Element("input");
  field.value = "Несохранённая строка";
  field.matches = () => true;
  field.blur = () => { doc.activeElement = null; field.blurred = true; };
  doc.activeElement = field;
  vm.runInNewContext(source("keyboard.js"), { window, document: doc });
  doc.dispatchEvent({ type: "focusin", target: field });
  return { doc, field };
}
const background = { type: "pointerdown", target: { closest: () => null } };

test("there is no floating hide-keyboard button covering the interface", () => {
  assert.doesNotMatch(source("index.html"), /keyboard-done|Сховати клавіатуру/);
});

test("background tap hides the keyboard with native Telegram dismissal and keeps text", async () => {
  let hidden = 0;
  const { doc, field } = keyboardFixture(() => hidden++);
  assert.equal(doc.documentElement.classList.contains("text-entry-active"), true);
  doc.dispatchEvent(background);
  assert.equal(hidden, 1);
  assert.equal(field.blurred, true);
  assert.equal(field.value, "Несохранённая строка");
  assert.equal(doc.documentElement.classList.contains("text-entry-active"), false);
});

test("keyboard dismissal works with older Telegram clients without hideKeyboard", async () => {
  const { doc, field } = keyboardFixture(undefined);
  doc.dispatchEvent(background);
  assert.equal(field.blurred, true);
});

test("a rejected native hideKeyboard call still blurs the field", async () => {
  const { doc, field } = keyboardFixture(() => { throw new Error("unsupported"); });
  doc.dispatchEvent({ type: "keydown", key: "Escape" });
  assert.equal(field.blurred, true);
});

test("taps on controls do not dismiss the keyboard", async () => {
  const { doc, field } = keyboardFixture();
  doc.dispatchEvent({ type: "pointerdown", target: { closest: () => ({}) } });
  assert.equal(field.blurred, undefined);
  doc.dispatchEvent(background);
  assert.equal(field.blurred, true);
});

test("editor still closes Telegram after a successful save when requested", async () => {
  const { get, isClosed } = await correctionFixture(true);
  get("corrected-text").value = "Віра 68";
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
  get("corrected-text").value = "Віра 68";
  await get("save-correction-button").fire("click");
  assert.equal(get("editor-audio").src, src);
  assert.equal(get("corrected-text").style.height, "100px");
  URL.revokeObjectURL(src);
});

test("editor explains when an old phrase has no linked audio", async () => {
  const { get } = await correctionFixture(false, false);
  assert.equal(get("editor-audio-load").disabled, true);
  assert.match(get("editor-audio-feedback").textContent, /не прив’язано/);
});

function gateFixture(fetch, { hasAuth = true } = {}) {
  const { doc, get } = documentFixture();
  const shell = new Element();
  const base = doc.querySelector;
  doc.querySelector = selector => selector === ".shell" ? shell : base(selector);
  const timers = [];
  const reload = { count: 0 };
  const context = {
    document: doc, fetch, AbortController, Error,
    window: {
      Telegram: hasAuth ? { WebApp: { initData: "signed-data" } } : null,
      setTimeout: (callback, ms) => { if (ms !== 15000) timers.push(callback); return timers.length; },
      clearTimeout: () => {},
      location: { reload: () => { reload.count++; } }
    }
  };
  vm.runInNewContext(source("client.js"), context);
  vm.runInNewContext(source("service-gate.js"), context);
  return { get, shell, timers, reload, client: context.window.Radio345 };
}

const offline = async () => { const error = new Error("Load failed"); error.name = "TypeError"; throw error; };
const online = async () => ({ ok: true, json: async () => ({ user_id: 1, role: "viewer" }) });

test("unavailable service shows only the gate, never the placeholder panel", async () => {
  const { get, shell, timers } = gateFixture(offline);
  await tick();
  assert.equal(shell.hidden, true);
  assert.equal(get("service-gate").hidden, false);
  assert.match(get("service-gate-title").textContent, /недоступний/);
  assert.equal(get("service-gate-retry").hidden, false);
  assert.equal(timers.length, 1, "автоматична повторна перевірка");
});

test("panel opens directly when the service answers", async () => {
  const { get, shell, reload } = gateFixture(online);
  await tick();
  assert.equal(shell.hidden, false);
  assert.equal(get("service-gate").hidden, true);
  assert.equal(reload.count, 0);
});

test("service coming back after an outage reloads into a clean panel", async () => {
  let up = false;
  const { timers, reload } = gateFixture((...args) => (up ? online : offline)(...args));
  await tick();
  up = true;
  timers.shift()();
  await tick();
  assert.equal(reload.count, 1);
});

test("forbidden user sees access message without retry loop", async () => {
  const { get, shell, timers } = gateFixture(async () => ({ ok: false, status: 403 }));
  await tick();
  assert.equal(shell.hidden, true);
  assert.match(get("service-gate-text").textContent, /\/start/);
  assert.equal(get("service-gate-retry").hidden, true);
  assert.equal(timers.length, 0);
});

test("outside Telegram the gate explains how to open the panel", async () => {
  let calls = 0;
  const { get, shell } = gateFixture(async () => { calls++; }, { hasAuth: false });
  await tick();
  assert.equal(shell.hidden, true);
  assert.match(get("service-gate-title").textContent, /Telegram/);
  assert.equal(calls, 0);
});

test("connection loss during work shows a banner but keeps the panel", async () => {
  let up = true;
  const { get, shell, client } = gateFixture((...args) => (up ? online : offline)(...args));
  await tick();
  up = false;
  await assert.rejects(client.request("/api/status"), error => error.unreachable === true);
  assert.equal(get("connection-lost").hidden, false);
  assert.equal(shell.hidden, false, "незбережений текст редактора лишається на екрані");
  up = true;
  await client.request("/api/status");
  assert.equal(get("connection-lost").hidden, true);
});

function notifyFixture(role, allowed) {
  const { doc, get } = documentFixture();
  const calls = [];
  const radios = ["commands", "watch", "all", "off"].map(value => {
    const radio = new Element("input");
    const option = new Element("label");
    radio.value = value;
    radio.closest = () => option;
    option.toggleAttribute = () => {};
    return radio;
  });
  doc.querySelectorAll = () => radios;
  doc.visibilityState = "visible";
  const context = {
    document: doc, Error,
    window: {
      Telegram: { WebApp: { initData: "signed-data" } },
      setInterval: () => 1, setTimeout, clearTimeout,
      Radio345: {
        user: async () => ({ role }),
        request: async (path, method, body) => {
          calls.push({ method, body });
          return method === "PUT" ? { mode: body.mode, allowed } : { mode: "commands", allowed };
        }
      }
    }
  };
  vm.runInNewContext(source("notifications.js"), context);
  return { get, calls, radios };
}

test("editor switches own mode with one tap; modes of other roles are hidden", async () => {
  const { get, calls, radios } = notifyFixture("editor", ["commands", "off", "watch"]);
  await tick();
  const all = radios.find(radio => radio.value === "all");
  assert.equal(all.disabled, true);
  assert.equal(all.closest().hidden, true);
  assert.equal(get("notification-mode-label").textContent, "Команди");
  const off = radios.find(radio => radio.value === "off");
  off.checked = true;
  await off.fire("change");
  const put = calls.find(call => call.method === "PUT");
  assert.equal(put.body.mode, "off");
  assert.equal(get("notification-mode-label").textContent, "Тиша");
  assert.match(get("notify-mode-feedback").textContent, /лише для вас/);
});

test("editor shows learning report and keeps window open for pending review", async () => {
  const { doc, get } = documentFixture();
  const calls = [];
  const report = { text_changed: true, format_only: false,
    candidates: [{ original: "пазіцію", corrected: "позицію", kind: "word", confirmations: 1, required: 2, approved: false, state: "waiting" }],
    review: [{ original: "вира", corrected: "віра", reason: "with_number", message: "назву змінено разом із номером", state: "pending" }],
    excluded: [{ label: "68 → 84", reason: "number", message: "змінено номер" }] };
  let closed = false;
  const context = {
    document: doc, URLSearchParams, URL, localStorage: { getItem: () => null, setItem() {} },
    window: {
      location: { search: "?fix_id=mem:" + "a".repeat(32) }, addEventListener() {},
      setTimeout: callback => callback(), Telegram: { WebApp: { close() { closed = true; } } },
      Radio345: { request: async (path, method, body) => {
        calls.push({ path, method, body });
        if (method === "GET") return { original_text: "Вира 68, іду на пазіцію", current_text: "Вира 68, іду на пазіцію", has_audio: false, learning: null };
        if (path === "/api/corrections/learning") return { ...report, review: [{ ...report.review[0], state: "confirmed" }] };
        return { corrected_text: body.corrected_text, saved: true, text_changed: true, learning: report };
      } },
    },
  };
  get("close-after-save").checked = true;
  vm.runInNewContext(source("correction-editor.js"), context);
  await tick();
  get("corrected-text").value = "Віра 84, іду на позицію";
  await get("save-correction-button").fire("click");
  await tick();
  const box = get("correction-learning");
  assert.equal(box.hidden, false);
  assert.equal(box.children[0].textContent, "Кандидатів для навчання: 1");
  assert.match(get("correction-feedback").textContent, /Перевір пари нижче/);
  assert.equal(closed, false, "вікно не закривається, поки є неперевірені пари");
  const reviewList = box.children[3];
  const confirm = reviewList.children[0].children[2].children[0];
  assert.equal(confirm.textContent, "Навчати");
  await confirm.fire("click");
  const decision = calls.find(call => call.path === "/api/corrections/learning");
  assert.deepEqual(JSON.parse(JSON.stringify(decision.body)), { fix_id: "mem:" + "a".repeat(32), expected_text: "Віра 84, іду на позицію",
    original: "вира", corrected: "віра", decision: "confirmed" });
});

test("viewer never requests personal notification controls", async () => {
  const { doc } = documentFixture();
  doc.querySelectorAll = () => [];
  const calls = [];
  vm.runInNewContext(source("notifications.js"), {document:doc,window:{
    Telegram:{WebApp:{initData:"signed-data"}},
    Radio345:{user:async()=>({role:"viewer"}),request:async (...args)=>calls.push(args)},
    setInterval(){}
  }});
  await tick();
  assert.equal(calls.length, 0);
});

test("leaving the edited text applies spelling and formatting fixes, and Save sends the fixed text", async () => {
  const { doc, get } = documentFixture();
  const calls = [];
  const context = {
    document: doc, URLSearchParams, URL, localStorage: { getItem: () => null, setItem() {} },
    window: {
      location: { search: "?fix_id=mem:" + "b".repeat(32) }, addEventListener() {},
      setTimeout: callback => callback(), clearTimeout() {}, Telegram: { WebApp: { close() {} } },
      Radio345: { request: async (path, method, body) => {
        calls.push({ path, method, body });
        if (method === "GET") return { original_text: "Вишня 48", current_text: "Вишня 48", has_audio: false, learning: null };
        if (path === "/api/corrections/check") {
          if (body.text === "вішня 48 прийом") return { issues: [{ kind: "spelling", message: "Можливо", fixed_text: "Вишня 48 прийом" }], formatted: null };
          if (body.text === "Вишня 48 прийом") return { issues: [{ kind: "format", message: "Коми" }], formatted: "Вишня 48, прийом" };
          return { issues: [], formatted: null };
        }
        return { corrected_text: body.corrected_text, saved: true, text_changed: true, learning: null };
      } },
    },
  };
  vm.runInNewContext(source("correction-editor.js"), context);
  await tick();
  const field = get("corrected-text");
  await field.fire("focus");
  field.value = "вішня 48 прийом";   // оператор дописав і помилився в назві
  await field.fire("input");
  await field.fire("blur", { relatedTarget: new Element("button") });
  await get("save-correction-button").fire("click");
  await tick();
  const saved = calls.find(call => call.path === "/api/corrections/draft" && call.method === "PUT");
  assert.equal(saved.body.corrected_text, "Вишня 48, прийом");
});

test("daily-code hints are not auto-corrected when leaving the field", async () => {
  const { doc, get } = documentFixture();
  const calls = [];
  const context = {
    document: doc, URLSearchParams, URL, localStorage: { getItem: () => null, setItem() {} },
    window: {
      location: { search: "?fix_id=mem:" + "c".repeat(32) }, addEventListener() {},
      setTimeout: callback => callback(), clearTimeout() {},
      Radio345: { request: async (path, method, body) => {
        calls.push({ path, method, body });
        if (method === "GET") return { original_text: "Береза 68, 48, 42", current_text: "Береза 68, 48, 42", has_audio: false };
        if (path === "/api/corrections/check") return {
          issues: [{ kind: "spelling", manual_only: true, message: "Можливо, код доби", fixed_text: "Береза 68, 48 62" }],
          formatted: null
        };
        return { corrected_text: body.corrected_text, saved: true, text_changed: true, learning: null };
      } },
    },
  };
  vm.runInNewContext(source("correction-editor.js"), context);
  await tick();
  const field = get("corrected-text");
  await field.fire("focus");
  field.value = "Береза 68, 48, 42";
  await field.fire("input");
  await field.fire("blur", { relatedTarget: new Element("button") });
  await get("save-correction-button").fire("click");
  const saved = calls.find(call => call.path === "/api/corrections/draft" && call.method === "PUT");
  assert.equal(saved.body.corrected_text, "Береза 68, 48, 42");
});

test("saving from the edited field does not trigger auto-correction", async () => {
  const { doc, get } = documentFixture();
  const calls = [];
  const context = {
    document: doc, URLSearchParams, URL, localStorage: { getItem: () => null, setItem() {} },
    window: {
      location: { search: "?fix_id=mem:" + "d".repeat(32) }, addEventListener() {},
      setTimeout: callback => callback(), clearTimeout() {},
      Radio345: { request: async (path, method, body) => {
        calls.push({ path, method, body });
        if (method === "GET") return { original_text: "Вишня 48", current_text: "Вишня 48", has_audio: false };
        if (path === "/api/corrections/check") return {
          issues: [{ kind: "spelling", message: "Можливо, «Вишня»", fixed_text: "Вишня 48" }],
          formatted: null
        };
        return { corrected_text: body.corrected_text, saved: true, text_changed: true, learning: null };
      } },
    },
  };
  vm.runInNewContext(source("correction-editor.js"), context);
  await tick();
  const field = get("corrected-text");
  await field.fire("focus");
  field.value = "Вішня 48";
  await field.fire("input");
  await field.fire("blur", { relatedTarget: get("save-correction-button") });
  await get("save-correction-button").fire("click");
  const saved = calls.find(call => call.path === "/api/corrections/draft" && call.method === "PUT");
  assert.equal(saved.body.corrected_text, "Вішня 48");
});
