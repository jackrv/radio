  // ---------- Користувачі ----------
  // ---------- Навчання за правками ----------
  // Чому навчена заміна ніколи не застосовується — простими словами.
  const BLOCK_REASONS = { service_word: "службове слово ефіру («прийом», «прийнято»…) — його майже завжди почуто правильно",
    number: "містить число — числа не замінюються", short_word: "закоротке слово", known_name: "почуте — назва зі словника",
    callsign_swap: "один позивний на інший", long_rewrite: "задовга заміна", format: "лише оформлення" };
  const LEARNING_QUEUE = ["needs_confirmation", "needs_approval", "conflict"];
  const LEARNING_KIND = { callsign: "позивний", word: "слово", blocked: "—" };
  let learningData = null;
  const learningSearch=el('input'); learningSearch.type='search'; learningSearch.placeholder='Знайти почуте або правильне слово';
  learningSearch.setAttribute('aria-label','Пошук навчених замін');
  $('learning-filter').before?.(learningSearch);
  learningSearch.addEventListener('input',()=>{if(learningData)renderLearning();});
  function learningState(item, need) {
    switch (item.state) {
      case "applies": return ["ok", "Діє в розпізнаванні"];
      case "needs_confirmation": {
        const left = need - item.confirmations;
        const tail = item.kind === "word" ? (item.approved ? " · дозвіл уже є" : " · і дозвіл") : "";
        return ["warn", `Ще ${left} ${left === 1 ? "однакова правка" : "однакові правки"}${tail}`];
      }
      case "needs_approval": return ["warn", "Чекає вашого дозволу"];
      case "conflict": return ["bad", "Те саме слово виправлено інакше в іншій правці"];
      case "disabled": return ["off", "Вимкнено"];
      default: return ["off", `Не застосовується: ${BLOCK_REASONS[item.blocked_reason] || item.blocked_reason || "заборонено перевіркою"}`];
    }
  }
  function learningSources(sources) {
    const box = el("span");
    for (const source of sources) {
      const event = /^db:(\d+)$/.exec(source);
      if (box.childNodes.length) box.append(", ");
      if (event) {
        const link = el("a", "", `№${event[1]}`);
        link.href = `?view=recordings&q=${encodeURIComponent("№" + event[1])}&filters=off`;
        link.title = "Знайти запис в «Аудіоархіві»";
        box.append(link);
      } else if (/^archive:[0-9a-f]{32}$/.test(source)) {
        const link=el('a','',`Архів ${source.slice(8,16)}`);
        link.href=`?view=recordings&q=${encodeURIComponent(source.slice(8))}&filters=off`;box.append(link);
      } else box.append(document.createTextNode("фраза Telegram"));
    }
    if (!sources.length) box.append(document.createTextNode("—"));
    return box;
  }
  async function changeRule(item, path, body) {
    await request(`/api/learning/${item.id}${path}`, "PUT", { ...body, expected_version: item.version });
    await learning();
  }
  function learningActions(item) {
    const box = el("span", "chip-row");
    if (item.kind === "word" && item.state !== "disabled") {
      const b = el("button", "outline-button", item.approved ? "Зняти дозвіл" : "Дозволити");
      b.title = item.approved ? "Заміна перестане діяти" : "Заміна діятиме, коли набере потрібні підтвердження";
      b.addEventListener("click", () => action(() => changeRule(item, "/approval", { approved: !item.approved })));
      box.append(b);
    }
    if (item.kind !== "blocked") {
      const off = item.state === "disabled";
      const b = el("button", "ghost", off ? "Увімкнути" : "Вимкнути");
      b.addEventListener("click", () => action(() => changeRule(item, "", { disabled: !off })));
      box.append(b);
    }
    return box;
  }
  function renderLearning() {
    const data = learningData, c = data.counts || {}, need = data.min_confirmations;
    let explanation=$('learning-explanation');
    if (!explanation) {
      explanation=el('p','muted small'); explanation.id='learning-explanation';
      $('learning-cards').before(explanation);
    }
    explanation.textContent=`Це словесні правила, а не дообучення аудіомоделі. Потрібно ${need} незалежних правок різних джерел; повторне збереження тієї самої фрази не додає голосу. Звичайні слова потребують окремого дозволу. Порожній список поточного фільтра не означає, що навчання вимкнене.`;
    const cards = $("learning-cards");
    const pick = value => () => {
      $("learning-filter").value = value;
      try { localStorage.setItem("radio345:learning-filter", value); } catch (_) { /* сесія */ }
      renderLearning();
    };
    const learningCard = (c, value, title) => { c.dataset.filter = value; return linked(c, pick(value), title); };
    cards.replaceChildren(
      learningCard(card("Діють у розпізнаванні", String(c.applies || 0), `з ${data.total} навчених замін`, (c.applies || 0) ? "ok" : ""), "applies", "Показати заміни, що діють"),
      learningCard(card("Чекають підтвердження", String(c.needs_confirmation || 0), `потрібно ${need} однакові правки в різних фразах`, (c.needs_confirmation || 0) ? "warn" : ""), "needs_confirmation", "Показати заміни, яким бракує правки"),
      learningCard(card("Чекають дозволу", String(c.needs_approval || 0), "підтверджено, але звичайне слово — потрібна ваша згода", (c.needs_approval || 0) ? "warn" : ""), "needs_approval", "Показати заміни, що чекають дозволу"),
      learningCard(card("Конфлікти", String(c.conflict || 0), "одне слово виправлено по-різному", (c.conflict || 0) ? "bad" : ""), "conflict", "Показати конфлікти"),
      learningCard(card("Вимкнені й заборонені", String((c.disabled || 0) + (c.blocked || 0)), `вимкнено: ${c.disabled || 0} · заборонено перевіркою: ${c.blocked || 0}`), "other", "Показати вимкнені й заборонені"),
    );
    const filter = $("learning-filter").value;
    for (const c of cards.children) c.classList.toggle("picked", c.dataset.filter === filter);
    const query=learningSearch.value.trim().toLocaleLowerCase('uk');
    const shown = data.items.filter(item => (filter === "all" || (filter === "queue" ? LEARNING_QUEUE.includes(item.state)
      : filter === "other" ? ["disabled", "blocked"].includes(item.state) : item.state === filter)) &&
      (!query || `${item.original} ${item.corrected}`.toLocaleLowerCase('uk').includes(query)));
    const table = $("learning-items"), head = el("tr");
    table.replaceChildren();
    for (const h of ["Почуте → правильне", "Тип", "Правок", "Стан", "Звідки", ""]) head.append(el("th", "", h));
    table.append(head);
    if (!shown.length) {
      const row = el("tr"), cell = el("td", "muted", query ? "За цим пошуком замін немає. Спробуйте інше слово або інший стан." : data.total ? "У цьому стані замін немає. Оберіть інший стан або «Усі»." : "Ще немає навчених замін. Виправте мовленнєвий запис в архіві або запустіть навчання за збереженою розміткою.");
      cell.colSpan = 6; row.append(cell); table.append(row);
    }
    for (const item of shown) {
      const [tone, text] = learningState(item, need);
      const row = el("tr"), pair = el("td"), state = el("td");
      pair.append(el("b", "", item.original), document.createTextNode(" → "), el("b", "", item.corrected));
      state.append(el("span", `pill ${tone}`, text));
      const sources = el("td"); sources.append(learningSources(item.sources));
      const actions = el("td", "nowrap"); actions.append(learningActions(item));
      row.append(pair, el("td", "", LEARNING_KIND[item.kind] || item.kind), el("td", "", `${item.confirmations} з ${need}`), state, sources, actions);
      table.append(row);
    }
  }
  async function learning(resume = true) {
    learningData = await request("/api/learning");
    renderLearning();
    if (resume) archiveLearningPanel().resume();
  }
  $("learning-filter").addEventListener("change", () => { if (learningData) renderLearning(); });
  $("learning-refresh").addEventListener("click", () => action(learning));

  const learnArchiveButton=el('button','ghost','Навчити за збереженою розміткою');learnArchiveButton.type='button';
  const learnArchiveResult=el('div');learnArchiveResult.setAttribute('role','status');
  $('learning-refresh').after?.(learnArchiveButton);
  document.querySelector('[data-panel="learning"]')?.append(learnArchiveResult);
  let archiveLearningTask=null;
  function archiveLearningPanel() {
    if (!archiveLearningTask) archiveLearningTask=serverJobPanel({button:learnArchiveButton,box:learnArchiveResult,kind:'archive_learning',
      start:()=>request('/api/learning/archive','POST'),render:r=>{
        learnArchiveResult.replaceChildren(el('p','small',`Синхронізовано ${r.synced}; помилок ${r.failed}. Резервна копія: ${r.backup}. Кількість пар не означає кількість увімкнених правил.`));
        void learning(false).catch(error=>note(error.message));
      }});
    return archiveLearningTask;
  }
  learnArchiveButton.addEventListener('click',()=>{
    if (!confirm('Навчити словесні правила за всіма збереженими еталонами «Мовлення»? Перед цим буде перевірена резервна копія events.db. Повтор не додасть зайвих голосів; пороги й окремий дозвіл слів збережуться.'))return;
    archiveLearningPanel().run();
  });

