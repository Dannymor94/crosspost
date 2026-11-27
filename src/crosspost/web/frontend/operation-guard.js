/* Shared T-216 editor guard. Never retry a POST automatically. */
(() => {
  "use strict";
  const hex = n => Array.from(crypto.getRandomValues(new Uint8Array(n)),
    b => b.toString(16).padStart(2, "0")).join("");
  const scope = (user, pid) => "crosspost:operation:v1:" + JSON.stringify([user.id, user.login, pid]);
  function read(key) {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const p = JSON.parse(raw);
    if (!p || !/^[0-9a-f]{32}$/.test(p.id) || !/^[0-9a-f]{64}$/.test(p.proof))
      throw new Error("Не удалось прочитать сохранённую операцию. Обратитесь к администратору.");
    return p;
  }
  const headers = p => ({"Idempotency-Key": p.id, "Idempotency-Recovery": p.proof,
    ...(p.draftScope ? {"X-Draft-Scope":p.draftScope,"X-Draft-Revision":p.id} : {})});
  function pending(key) {
    try { return !!read(key); } catch { return true; }
  }
  // Bind recovery validation to the actual submitted selection, not the current editor.
  const targetKey = (channel, target) => JSON.stringify([channel, target]);
  const statuses = new Set(["done","submitted","failed","needs_relogin","deferred","banned"]);
  const scheduleStatuses = new Set(["scheduled","pending_approval"]);
  function publicationExpectation(selections, article = false) {
    const targets = selections.map(s => typeof s === "string"
      ? targetKey(s, "") : targetKey(s.channel, String(s.target_id || "")));
    if (article) targets.push(targetKey("telegraph", ""));
    return {kind:"immediate", targets:[...new Set(targets)].sort()};
  }
  function validOutcomes(rows, expected) {
    if (expected?.kind !== "immediate" || !Array.isArray(expected.targets) ||
        !expected.targets.length || !Array.isArray(rows) || rows.length !== expected.targets.length)
      return false;
    const remaining = new Set(expected.targets);
    return remaining.size === rows.length && rows.every(row =>
      row && typeof row.channel === "string" && typeof row.target === "string" &&
      statuses.has(row.status) && remaining.delete(targetKey(row.channel, row.target)));
  }
  function validScheduled(row, id) {
    return !!row && Number.isSafeInteger(id) && id > 0 && scheduleStatuses.has(row.status);
  }
  function validDirect(result, expected) {
    if (expected?.kind === "scheduled") return validScheduled(result, result?.id);
    return typeof result?.publication_id === "string" && !!result.publication_id.trim() &&
      validOutcomes(result.outcomes, expected);
  }
  async function run(key, send, expected, draft = null) {
    if (!navigator.locks || !crypto.getRandomValues)
      throw new Error("Для безопасной отправки нужен современный браузер и HTTPS.");
    return navigator.locks.request(key, {ifAvailable: true}, async lock => {
      if (!lock || read(key)) throw new Error("Сначала проверьте предыдущую операцию.");
      if (!["immediate","scheduled"].includes(expected?.kind)) throw new Error("Не задан ожидаемый результат операции.");
      if (draft && (!/^[0-9a-f]{64}$/.test(draft.scope) || !/^[0-9a-f]{32}$/.test(draft.revision)))
        throw new Error("Сохраните черновик перед отправкой.");
      const p = {id: draft ? draft.revision : hex(16), proof: hex(32), expected,
        ...(draft ? {draftScope:draft.scope} : {})};
      localStorage.setItem(key, JSON.stringify(p)); // before network, fail closed
      const result = await send(headers(p));
      if (!validDirect(result, expected)) throw new Error("Неполная или неизвестная квитанция. Проверьте операцию.");
      // Retain pending on any ambiguous error; only validated success clears.
      // A bound editor retains proof until clearing its exact draft or acknowledging.
      if (!draft && read(key)?.id === p.id) localStorage.removeItem(key);
      return result;
    });
  }
  async function recover(key, base) {
    const p = read(key);
    if (!p) throw new Error("Нет сохранённой операции.");
    const r = await fetch(base + "/operation-status", {
      credentials: "same-origin", cache: "no-store", headers: headers(p)
    });
    if (!r.ok) throw new Error("Результат пока недоступен. Не отправляйте повторно.");
    const data = await r.json();
    const rows = Array.isArray(data.outcomes) ? data.outcomes : [];
    if (p.expected?.kind === "scheduled" && data.state === "scheduled_created" && rows.length === 1 &&
        typeof rows[0]?.scheduled_id === "string" && /^[1-9][0-9]*$/.test(rows[0].scheduled_id) &&
        validScheduled(rows[0], Number(rows[0].scheduled_id))) {
      return {known: true, id: p.id, message: "Пост был создан: " +
        (rows[0].status === "pending_approval" ? "на проверке" : "в расписании") + ". Проверьте актуальный статус в публикациях."};
    }
    if (data.state === "recorded" && validOutcomes(rows, p.expected)) {
      const labels = {done:"опубликовано", submitted:"без подтверждения", failed:"ошибка",
        needs_relogin:"нужен вход", deferred:"отложено", banned:"канал заблокирован"};
      return {known: true, id: p.id, message: rows.map(o =>
        o.channel + (o.target ? " · " + o.target : "") + ": " + (labels[o.status] || "неизвестно")).join("; ")};
    }
    return {known: false, id: p.id, message: "Результат неизвестен. Проверьте площадки или обратитесь к администратору. Повторная отправка заблокирована."};
  }
  async function acknowledge(key, receipt) {
    if (!receipt?.known || !navigator.locks) return;
    await navigator.locks.request(key, {ifAvailable: true}, lock => {
      if (lock && read(key)?.id === receipt.id) localStorage.removeItem(key);
    });
  }
  window.CrosspostOperations = Object.freeze({scope, pending, run, recover, acknowledge, publicationExpectation});
})();
