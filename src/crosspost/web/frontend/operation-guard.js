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
  const headers = p => ({"Idempotency-Key": p.id, "Idempotency-Recovery": p.proof});
  function pending(key) {
    try { return !!read(key); } catch { return true; }
  }
  async function run(key, send) {
    if (!navigator.locks || !crypto.getRandomValues)
      throw new Error("Для безопасной отправки нужен современный браузер и HTTPS.");
    return navigator.locks.request(key, {ifAvailable: true}, async lock => {
      if (!lock || read(key)) throw new Error("Сначала проверьте предыдущую операцию.");
      const p = {id: hex(16), proof: hex(32)};
      localStorage.setItem(key, JSON.stringify(p)); // before network, fail closed
      const result = await send(headers(p));
      if (!result || typeof result !== "object") throw new Error("Ответ не получен.");
      // Retain pending on any ambiguous error; only validated success clears.
      if (read(key)?.id === p.id) localStorage.removeItem(key);
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
    if (data.state === "scheduled_created" && rows.length === 1 && rows[0].scheduled_id) {
      return {known: true, id: p.id, message: "Пост был создан: " +
        (rows[0].status === "pending_approval" ? "на проверке" : "в расписании") + ". Проверьте актуальный статус в публикациях."};
    }
    if (data.state === "recorded" && rows.length) {
      const labels = {done:"опубликовано", submitted:"без подтверждения", failed:"ошибка",
        needs_relogin:"нужен вход", deferred:"отложено"};
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
  window.CrosspostOperations = Object.freeze({scope, pending, run, recover, acknowledge});
})();
