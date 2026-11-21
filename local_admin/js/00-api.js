  // Транспорт API: жодного DOM, правил розпізнавання чи керування вкладками.
  const pendingReads = new Map();
  async function request(path, method = "GET", body, headers = {}) {
    if (method !== "GET") return fetchPanel(path, method, body, headers);
    const key = JSON.stringify([path, Object.entries(headers).sort()]);
    let pending = pendingReads.get(key);
    if (!pending) {
      pending = fetchPanel(path, method, body, headers);
      pendingReads.set(key, pending);
      const clear = () => { if (pendingReads.get(key) === pending) pendingReads.delete(key); };
      pending.then(clear, clear);
    }
    const value = await pending;
    // Відповідь може змінювати контролер конкретного розділу; спільного mutable стану немає.
    if (!value || typeof value !== "object" || value instanceof Blob) return value;
    return typeof structuredClone === "function" ? structuredClone(value) : JSON.parse(JSON.stringify(value));
  }
  async function fetchPanel(path, method = "GET", body, headers = {}) {
    const options = { method, headers, credentials: "same-origin", cache: "no-store" };
    if (body !== undefined) {
      if (body instanceof Blob) options.body = body;
      else { options.body = JSON.stringify(body); options.headers = { ...headers, "Content-Type": "application/json" }; }
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(path, {...options, signal: controller.signal});
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        const failure = new Error(typeof error.detail === "string" ? error.detail : `HTTP ${response.status}. Онови сторінку або відкрий панель командою --open.`);
        failure.status = response.status;
        throw failure;
      }
      if (response.status === 204) return null;
      return await ((response.headers.get("content-type") || "").includes("json") ? response.json() : response.blob());
    } catch (error) {
      if (error.name === "AbortError") error = new Error("Сервер не відповів за 30 с. Перевірте стан перед повтором.");
      // Відсутність відповіді не доводить, що сервер не виконав запис.
      if (!["GET", "HEAD"].includes(method) && !error.status) {
        error.uncertain = true;
        error.message += " Результат операції невідомий; перевірте збережені дані або список завдань.";
      }
      throw error;
    } finally { clearTimeout(timeout); }
  }
