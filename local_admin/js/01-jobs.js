  // Довгий прогін належить серверу, а не відкритій вкладці. Тут лише короткі запити стану.
  function serverJobPanel({button, box, kind, listType = null, start, render}) {
    const idleLabel=button.textContent;
    let timer = null, generation = 0;
    const connected = () => box.isConnected !== false;
    const later = (id, token) => { timer = setTimeout(() => poll(id, token), 2500); };
    function show(job, token) {
      if (token !== generation || !connected()) return;
      const active = ["queued", "running"].includes(job.status);
      button.disabled = active;
      button.textContent=active ? (job.status==='queued' ? 'У черзі…' : 'Виконується…') : idleLabel;
      if (job.status === "done") {
        render(job.result);
        box.prepend(el("p", "muted small", `Збережений результат від ${new Date(job.finished * 1000).toLocaleString("uk-UA")}. ${["impact", "evaluate"].includes(kind) ? "Перевірено знімок даних на час запуску; пізніші правки не враховані." : "Збережено."}`));
      } else {
        const p = job.progress || {}, done = p.total ? ` · ${Math.min(100, Math.floor(100 * p.done / p.total))}% (${p.done} / ${p.total})` : "";
        const states = {queued:"У черзі", running:"Виконую завдання", cancelled:"Скасовано", interrupted:"Перервано", failed:"Помилка"};
        box.replaceChildren(el("p", active ? "muted small" : "alert small", `${states[job.status] || job.status}${done}. ${job.error || (active ? "Можна закрити сторінку — перевірка продовжиться, результат збережеться." : "Можна запустити повторно.")}`));
        if (active && (job.status === "queued" || job.cancellable !== false)) {
          const stop = el("button", "ghost small-button", "Скасувати завдання"); stop.type = "button";
          stop.addEventListener("click", async () => {
            try { await request(`/api/jobs/${job.id}/cancel`, "POST"); await poll(job.id, token); }
            catch (error) { note(error.message); }
          });
          box.append(stop);
        }
      }
      if (active) later(job.id, token);
    }
    async function poll(id, token) {
      clearTimeout(timer);
      if (token !== generation || !connected()) return;
      try { show(await request(`/api/jobs/${id}`), token); }
      catch (error) {
        if (token !== generation || !connected()) return;
        if ([401, 403, 404].includes(error.status)) {
          button.disabled = false;
          box.replaceChildren(el("p", "alert small", `${error.message} Оновіть вхід або запустіть нову перевірку.`));
          return;
        }
        box.replaceChildren(el("p", "alert small", `${error.message} Відновлюю зв'язок; серверне завдання не скасовується.`));
        later(id, token);
      }
    }
    return {
      async run() {
        const token = ++generation; clearTimeout(timer); button.disabled = true;
        box.replaceChildren(el("p", "muted small", "Ставлю завдання в чергу…"));
        try { show(await start(), token); }
        catch (error) { if (token === generation) { button.disabled = false; box.replaceChildren(el("p", "alert small", error.message)); } }
      },
      async resume() {
        const token = ++generation; clearTimeout(timer);
        try {
          const jobs = await request("/api/jobs");
          const job = jobs.items.find(j => j.kind === kind && (j.list_type || null) === listType);
          if (job) await poll(job.id, token);
        } catch (_) { /* Вхід ще не завершено або сервер тимчасово недоступний. */ }
      },
    };
  }
