/* Profile timezone conversion: server resolves wall time, no browser DST guesses. */
(() => {
  "use strict";
  async function request(url, options) {
    const response = await fetch(url, {credentials: "same-origin", cache: "no-store", ...options});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Не удалось проверить часовой пояс.");
    return body;
  }
  const get = async base => (await request(base + "/settings/timezone")).zone;
  const set = async (base, zone) => (await request(base + "/settings/timezone", {
    method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({zone})
  })).zone;
  const resolve = async (base, wall, zone) => {
    if (!zone || !wall) throw new Error("Укажите время и настройте часовой пояс профиля.");
    return (await request(base + "/scheduled/resolve-time", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({wall, zone})
    })).instant;
  };
  const deviceZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;
  function wallInput(value, zone) {
    if (!value) return "";
    if (!/(Z|[+-]\d\d:\d\d)$/.test(value)) return value.slice(0, 16);
    if (!zone) return "";
    const parts = new Intl.DateTimeFormat("en-CA", {timeZone: zone, year:"numeric", month:"2-digit",
      day:"2-digit", hour:"2-digit", minute:"2-digit", hourCycle:"h23"}).formatToParts(new Date(value));
    const p = Object.fromEntries(parts.map(x => [x.type, x.value]));
    return p.year + "-" + p.month + "-" + p.day + "T" + p.hour + ":" + p.minute;
  }
  function label(value, zone) {
    if (!value) return "";
    if (!/(Z|[+-]\d\d:\d\d)$/.test(value)) return value.slice(0,16).replace("T"," ") + " · старое время сервера";
    return zone ? wallInput(value, zone).replace("T"," ") + " · " + zone : value + " · UTC";
  }
  window.CrosspostTime = Object.freeze({get, set, resolve, deviceZone, wallInput, label});
})();
