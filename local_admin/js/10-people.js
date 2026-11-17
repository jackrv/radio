  // ---------- Активність користувача ----------
  const ACTIONS = {
    open: "Відкрито Mini App", correction: "Правка тексту", learning: "Рішення щодо навчання",
    mode: "Режим сповіщень", receiver: "Керування приймачем", dictionary: "Словник",
    rule: "Правило слів", rule_approval: "Дозвіл правила", user: "Зміна користувача",
    settings: "Налаштування розпізнавання", sample: "Зразок сигналу",
    delete: "Видалено повідомлення в себе", hide: "Приховано фразу для всіх",
    recall: "Видалено в Telegram у всіх (панель)", panel: "Дія в локальній панелі",
  };
  const DECISIONS_UA = { confirmed: "навчати", rejected: "не навчати", pending: "без рішення" };
  const when = ts => ts ? new Date(ts * 1000).toLocaleString("uk-UA") : "—";
  function activityTarget(item) {
    const event = /^db:(\d+)$/.exec(item.target || "");
    if (event) {
      const link = el("a", "", `№${event[1]}`);
      link.href = `?view=recordings&q=${encodeURIComponent("№" + event[1])}`;
      link.title = "Знайти запис в «Аудіоархіві»";
      return link;
    }
    if (item.action === "mode") return document.createTextNode(MODES[item.target] || item.target);
    return document.createTextNode(item.target || "");
  }
  function activityDetails(item) {
    const d = item.details || {};
    switch (item.action) {
      case "correction": return d.changed ? `«${d.from}» → «${d.to}»` : "текст не змінено";
      case "learning": return `«${d.original}» → «${d.corrected}»: ${DECISIONS_UA[d.decision] || d.decision}`;
      case "mode": return d.via === "bot" ? "командою в боті" : "у Mini App";
      case "receiver": return d.minutes ? `пауза на ${d.minutes} хв` : "приймання відновлено";
      case "dictionary": return `діючих рядків: ${d.lines}`;
      case "rule": return d.disabled ? "вимкнено" : "увімкнено";
      case "rule_approval": return d.approved ? "дозволено" : "дозвіл знято";
      case "user": return [d.role && ROLES[d.role], d.enabled === false ? "вимкнено" : "", d.notify_mode && MODES[d.notify_mode]].filter(Boolean).join(" · ");
      default: return "";
    }
  }
  // Вибраний користувач запам’ятовується в цьому браузері.
  const ACTIVITY_KEY = "radio345.activityUser";
  let activityFilter = "all", activityRevision = 0, selectedActivityUser = "";
  const remembered = () => { try { return localStorage.getItem(ACTIVITY_KEY) || ""; } catch (_) { return ""; } };
  async function activity(userId) {
    const revision = ++activityRevision;
    userId = userId || selectedActivityUser || remembered();
    let result;
    try { result = await request(`/api/activity${userId ? `?user_id=${encodeURIComponent(userId)}` : ""}`); }
    catch (error) {
      if (revision !== activityRevision) return;
      if (!userId || error.status !== 404) throw error;
      try { localStorage.removeItem(ACTIVITY_KEY); } catch (_) {}
      result = await request("/api/activity");   // користувача вже немає — перший у списку
    }
    if (revision !== activityRevision) return;
    const select = $("activity-user");
    const s = result.selected;
    const signature = JSON.stringify(result.users);
    if (select.dataset.users !== signature) {
      select.replaceChildren(); select.dataset.users = signature;
      for (const u of result.users) {
        const button = el("button", "ghost", `${u.name || u.user_id} · ${ROLES[u.role] || u.role}${u.enabled ? "" : " · вимкнений"}`);
        button.type = "button"; button.dataset.userId = String(u.user_id);
        button.addEventListener("click", () => {
          activityFilter = "all";
          activity(String(u.user_id)).catch(error => note(error.message));
        });
        select.append(button);
      }
    }
    const cards = $("activity-cards"), table = $("activity-items");
    const wrap = table.parentElement, scroll = wrap?.scrollTop;
    cards.replaceChildren(); table.replaceChildren();
    if (!s) { selectedActivityUser = ""; cards.append(el("p", "muted", "Користувачів ще немає.")); return; }
    selectedActivityUser = String(s.user_id);
    try { localStorage.setItem(ACTIVITY_KEY, selectedActivityUser); } catch (_) {}
    for (const button of select.children) {
      const chosen = button.dataset.userId === selectedActivityUser;
      button.classList.toggle("on", chosen); button.setAttribute("aria-pressed", String(chosen));
    }
    const a = s.actions || {}, w = s.actions_week || {};
    const other = Object.entries(a).filter(([k]) => !["open", "correction", "learning", "mode"].includes(k)).reduce((n, [, v]) => n + v, 0);
    const MAIN = ["open", "correction", "learning"];
    const filters = { all: () => true, correction: i => i.action === "correction", learning: i => i.action === "learning",
                      open: i => i.action === "open", other: i => !MAIN.includes(i.action) };
    const FILTER_NAMES = { correction: "правки тексту", learning: "рішення щодо навчання", open: "відкриття Mini App", other: "інші дії" };
    const filterBy = key => { activityFilter = key; renderItems(); };
    const openEdits = () => {
      // Архів: усі записи з правками цього користувача.
      const select = $("recording-edits"), id = String(s.user_id);
      if (![...select.options].some(o => o.value === id)) { const o = el("option", "", `Правки: ${(result.users.find(u => String(u.user_id) === id)?.name || id)}`); o.value = id; select.append(o); }
      select.value = id; $("recording-view").value = "all"; $("recording-period").value = "";
      if (loaded.has("recordings")) { show("recordings"); action(() => recordings(true)); } else show("recordings");
    };
    const filterCard = (c, key, title) => { c.dataset.filter = key; return linked(c, () => filterBy(key), title); };
    cards.append(
      linked(card("Роль", ROLES[s.role] || s.role, `${s.enabled ? "активний" : "вимкнений"} · доступ з ${s.created_at || "—"}`, s.enabled ? "ok" : "bad"), () => show("users"), "Змінити роль і доступ"),
      linked(card("Сповіщення", MODES[s.notify_mode] || s.notify_mode || "—", "поточний режим"), () => show("users"), "Змінити режим сповіщень"),
      filterCard(card("Доставлено сповіщень", String(s.delivered.total), Object.entries(s.delivered.by_kind || {}).map(([k, v]) => `${k}: ${v}`).join(" · ") || "за весь час"), "all", "Показати всі дії"),
      linked(card("Правки тексту", String(a.correction || 0), `зі змінами: ${s.corrections_changed} · за 7 днів: ${w.correction || 0}`), openEdits, "Записи архіву з правками цього користувача"),
      filterCard(card("Рішення щодо навчання", String(a.learning || 0), `за 7 днів: ${w.learning || 0}`), "learning", "Показати лише рішення щодо навчання"),
      filterCard(card("Відкриття Mini App", String(a.open || 0), `останнє: ${when(s.last_activity)}`), "open", "Показати лише відкриття Mini App"),
      filterCard(card("Активних днів", String(s.active_days || 0), s.first_logged ? `журнал з ${when(s.first_logged)}` : "дій ще не записано"), "all", "Показати всі дії"),
      filterCard(card("Інші дії", String(other + (a.mode || 0)), `режим: ${a.mode || 0} · словники, правила, налаштування: ${other}`), "other", "Показати лише інші дії"),
    );
    function renderItems() {
      for (const c of cards.children) c.classList?.toggle("picked", !!c.dataset?.filter && c.dataset.filter === activityFilter && activityFilter !== "all");
      table.replaceChildren();
      const items = s.items.filter(filters[activityFilter] || filters.all);
      const head = el("tr");
      for (const h of ["Час", "Дія", "Об’єкт", "Подробиці"]) head.append(el("th", "", h));
      table.append(head);
      if (activityFilter !== "all") {
        const row = el("tr"), cell = el("td", "small muted");
        cell.colSpan = 4;
        const reset = el("button", "ghost", "показати всі"); reset.type = "button";
        reset.addEventListener("click", () => filterBy("all"));
        cell.append(`Показано: ${FILTER_NAMES[activityFilter]} (${items.length}) · `, reset);
        row.append(cell); table.append(row);
      }
      if (!items.length) {
        const row = el("tr"), cell = el("td", "muted", s.items.length ? "Таких дій серед останніх немає." : "Дій ще не записано. Вони з’являться, щойно користувач відкриє Mini App чи щось змінить.");
        cell.colSpan = 4; row.append(cell); table.append(row);
      }
      for (const item of items) {
        const row = el("tr"), target = el("td");
        target.append(activityTarget(item));
        row.append(el("td", "nowrap", when(item.ts)), el("td", "", ACTIONS[item.action] || item.action), target, el("td", "", activityDetails(item)));
        table.append(row);
      }
    }
    renderItems();
    if (wrap && scroll != null) wrap.scrollTop = scroll;
  }


  async function users() {
    const result = await request("/api/users");
    const table = $("users"), head = el("tr");
    table.replaceChildren();
    for (const h of ["Користувач", "Назва", "Роль", "Сповіщення", "Доступ", ""]) head.append(el("th", "", h));
    table.append(head);
    for (const u of result.items) {
      const row = el("tr"), who = el("td");
      who.append(el("div", "", u.telegram_name || (u.username ? "@" + u.username : "—")),
        el("div", "muted small", `${u.username && u.telegram_name ? "@" + u.username + " · " : ""}${u.user_id}`));
      const name = el("input"); name.maxLength = 80; name.value = u.display_name || ""; name.placeholder = "ім’я з Telegram";
      const role = el("select");
      for (const [k, v] of Object.entries(ROLES)) { const o = el("option", "", v); o.value = k; role.append(o); }
      role.value = u.role;
      const mode = el("select");
      for (const [k, v] of Object.entries(MODES)) { const o = el("option", "", v); o.value = k; mode.append(o); }
      mode.value = u.notify_mode;
      const restrict = () => {
        for (const o of mode.options) o.disabled = o.value === "all" && role.value !== "admin";
        if (mode.value === "all" && role.value !== "admin") mode.value = "commands";
      };
      role.addEventListener("change", restrict); restrict();
      const enabled = el("input"); enabled.type = "checkbox"; enabled.checked = u.enabled;
      const access = el("label", "inline"); access.append(enabled, document.createTextNode(" увімкнено"));
      const save = el("button", "", "Зберегти");
      save.addEventListener("click", () => action(async () => {
        if (u.role === "admin" && (role.value !== "admin" || !enabled.checked)
            && !confirm("Забрати права адміністратора чи доступ? Якщо це ваш обліковий запис, керувати з Mini App ви вже не зможете.")) return;
        await request(`/api/users/${u.user_id}`, "PUT", { role: role.value, enabled: enabled.checked,
          display_name: name.value, notify_mode: mode.value, expected_version: u.version });
        await users(); note("Користувача збережено.");
      }));
      const cells = [who, el("td"), el("td"), el("td"), el("td"), el("td")];
      cells[1].append(name); cells[2].append(role); cells[3].append(mode); cells[4].append(access); cells[5].append(save);
      row.append(...cells); table.append(row);
    }
  }
  $("users-refresh").addEventListener("click", () => action(users));
  $("new-user-role").addEventListener("change", () => {
    const mode = $("new-user-mode"), admin = $("new-user-role").value === "admin";
    for (const o of mode.options) o.disabled = o.value === "all" && !admin;
    if (mode.value === "all" && !admin) mode.value = "commands";
  });
  $("user-add").addEventListener("submit", event => { event.preventDefault(); action(async () => {
    const id = $("new-user-id").value.trim();
    if (!/^[1-9][0-9]{0,18}$/.test(id)) throw new Error("Telegram ID — лише цифри");
    await request(`/api/users/${id}`, "PUT", { role: $("new-user-role").value, enabled: true,
      display_name: $("new-user-name").value, notify_mode: $("new-user-mode").value, expected_version: null });
    $("new-user-id").value = ""; $("new-user-name").value = "";
    await users(); note(`Доступ для ${id} надано.`);
  }); });
  $("new-user-role").dispatchEvent?.(new Event("change"));

