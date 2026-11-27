/* Page-level theme only; never reads actor, profile or publication data. */
(() => {
  "use strict";
  const root = document.documentElement;
  const theme = window.CrosspostTheme.mount(root);
  window.CrosspostPageTheme = theme;
  new MutationObserver(() => {
    document.querySelectorAll("[data-cp-theme-select]").forEach(select => {
      select.value = theme.preference;
    });
  }).observe(root, {attributes:true, attributeFilter:["data-theme-preference"]});
})();
