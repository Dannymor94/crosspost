/* Presentation-only navigation: host supplies authorized destinations and handles navigation. */
(() => {
  "use strict";
  const PATHS = Object.freeze({
    home: "M3 10 12 3l9 7v11h-6v-7H9v7H3z",
    calendar: "M4 5h16v16H4z M4 10h16 M8 3v4 M16 3v4 M8 14h2 M14 14h2",
    posts: "M5 3h14v18H5z M8 8h8 M8 12h8 M8 16h6",
    moderation: "M12 3 3 7v5c0 5 4 8 9 10 5-2 9-5 9-10V7z M8 12l3 3 5-6",
    clients: "M16 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2 M10 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M18 8v6 M15 11h6",
    connections: "M7 7h11l-3-3 M18 7l-3 3 M17 17H6l3 3 M6 17l3-3",
    more: "M4 12h1 M11.5 12h1 M19 12h1",
  });
  const SVG_NS = "http://www.w3.org/2000/svg";
  function icon(id) {
    const span = document.createElement("span");
    span.className = "cp-nav-icon";
    span.setAttribute("aria-hidden", "true");
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", Object.hasOwn(PATHS, id) ? PATHS[id] : PATHS.more);
    svg.append(path);
    span.append(svg);
    return span;
  }
  function mount(root, {items, active = null, onNavigate, label = "Навигация"}) {
    // All destinations, including mobile subset, are provided by the host.
    // No implicit routes, redirects, fetches or permission decisions here.
    if (!Array.isArray(items) || typeof onNavigate !== "function") throw new TypeError("Invalid navigation");
    const seen = new Set();
    const validated = items.map(item => {
      if (!item || typeof item.id !== "string" || !item.id || seen.has(item.id)
          || typeof item.label !== "string" || !item.label.trim()) throw new TypeError("Invalid destination");
      seen.add(item.id);
      return {id: item.id, label: item.label, icon: item.icon || item.id};
    });
    let disposed = false;
    const nav = document.createElement("nav");
    nav.className = "cp-nav";
    nav.setAttribute("aria-label", label);
    const buttons = new Map();
    for (const item of validated) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "cp-button cp-nav-item";
      button.dataset.destination = item.id;
      const title = document.createElement("span");
      title.textContent = item.label;
      button.append(icon(item.icon), title);
      button.addEventListener("click", () => { if (!disposed) onNavigate(item.id); });
      nav.append(button);
      buttons.set(item.id, button);
    }
    function setActive(id) {
      if (disposed || (id !== null && !buttons.has(id))) return false;
      for (const [key, button] of buttons) {
        if (key === id) button.setAttribute("aria-current", "page");
        else button.removeAttribute("aria-current");
      }
      return true;
    }
    if (!setActive(active)) throw new TypeError("Unknown active destination");
    root.replaceChildren(nav);
    return Object.freeze({
      setActive,
      dispose() { disposed = true; if (nav.parentNode === root) nav.remove(); }
    });
  }
  window.CrosspostNavigation = Object.freeze({mount});
})();
