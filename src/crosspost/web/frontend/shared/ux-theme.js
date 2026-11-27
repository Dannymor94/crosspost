/* Shared theme preference only. No account data, API or framework dependencies. */
(() => {
  "use strict";
  const KEY = "crosspost.ux.theme.v1";
  const valid = value => ["system", "light", "dark"].includes(value);
  function mount(root, options = {}) {
    const media = options.media || window.matchMedia("(prefers-color-scheme: dark)");
    // Access to localStorage itself may throw (private or restricted contexts).
    let storage = null;
    try { storage = options.storage === undefined ? window.localStorage : options.storage; }
    catch (_) {}
    let preference = "system";
    try {
      const value = storage?.getItem(KEY);
      if (valid(value)) preference = value;
    } catch (_) {}
    let disposed = false;
    function apply() {
      if (disposed) return;
      root.dataset.theme = preference === "system" ? (media.matches ? "dark" : "light") : preference;
      root.dataset.themePreference = preference;
    }
    function sync(event) {
      if (event.key !== KEY && event.key !== null) return;
      preference = valid(event.newValue) ? event.newValue : "system";
      apply();
    }
    media.addEventListener("change", apply);
    window.addEventListener("storage", sync);
    apply();
    return Object.freeze({
      get preference() { return preference; },
      set(value) {
        if (disposed || !valid(value)) return false;
        preference = value;
        try { storage?.setItem(KEY, value); } catch (_) {}
        apply();
        return true;
      },
      dispose() {
        disposed = true;
        media.removeEventListener("change", apply);
        window.removeEventListener("storage", sync);
      },
    });
  }
  window.CrosspostTheme = Object.freeze({mount});
})();
