/* T-218: shared target intersection and latest-request fencing. */
(() => {
  "use strict";
  function intersection(keys, targets, keyOf) {
    const allowed = new Set(targets.filter(t => t.eligible).map(keyOf));
    const retained = [], removed = [];
    for (const key of keys) (allowed.has(key) ? retained : removed).push(key);
    return {retained, removed};
  }
  function latest() {
    let version = 0;
    return {
      begin() { const current = ++version; return () => current === version; },
      invalidate() { version++; }
    };
  }
  window.CrosspostEditorTargets = Object.freeze({intersection, latest});
})();
