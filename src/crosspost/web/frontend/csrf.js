/* One shared same-origin request helper; no persistent token, no automatic retry. */
(() => {
  "use strict";
  let pending = null;
  function local(url) {
    const target = new URL(url, location.origin);
    if (target.origin !== location.origin) throw Error("Недопустимый адрес запроса");
    return target;
  }
  async function proof() {
    // Coalesce initial requests within this page so they use the same nonce.
    if (!pending) {
      pending = (async () => {
        const response = await fetch("/api/auth/csrf", {
          credentials: "same-origin", cache: "no-store"
        });
        if (!response.ok) throw Error("Не удалось подтвердить безопасность запроса");
        const data = await response.json();
        if (data.enabled === false) return null;
        if (data.enabled !== true || !/^[a-f0-9]{64}$/.test(data.token || "")) {
          throw Error("Не удалось подтвердить безопасность запроса");
        }
        return data.token;
      })().finally(() => { pending = null; });
    }
    return pending;
  }
  async function request(url, options = {}) {
    const target = local(url);
    const method = (options.method || "GET").toUpperCase();
    if (["GET","HEAD","OPTIONS"].includes(method)) return fetch(target, options);
    const headers = new Headers(options.headers || {});
    const token = await proof();
    if (token) headers.set("X-CSRF-Token", token);
    return fetch(target, {...options, headers, credentials:"same-origin"});
  }
  window.CrosspostRequest = Object.freeze({fetch: request, proof});
})();
