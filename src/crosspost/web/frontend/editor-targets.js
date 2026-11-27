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
  function selection(keys,targets,keyOf,state) {
    if(state!=="ready") return {available:[],selected:[],unavailable:0};
    const available=[...new Set(targets.filter(t=>t.eligible===true).map(keyOf))];
    const allowed=new Set(available);
    return {available,selected:[...new Set(keys)].filter(k=>allowed.has(k)),
      unavailable:targets.filter(t=>t.eligible!==true).length};
  }
  function createToolbar(h) {
    return function TargetToolbar({summary,locked,onSelect}) {
      return h("div",{className:"cp-panel cp-stack cp-target-toolbar"},
        h("p",{role:"status"},"Выбрано целей: "+summary.selected.length+" из "+summary.available.length),
        h("div",{className:"cp-row"},
          h("button",{type:"button",className:"cp-button",disabled:locked||!summary.available.length||summary.selected.length===summary.available.length,
            onClick:()=>onSelect(summary.available)},"Выбрать все доступные"),
          h("button",{type:"button",className:"cp-button",disabled:locked||!summary.selected.length,
            onClick:()=>onSelect([])},"Снять выбор")),
        summary.unavailable?h("p",{className:"cp-muted"},"Недоступных целей: "+summary.unavailable+". Они не будут выбраны."):null);
    };
  }
  window.CrosspostEditorTargets = Object.freeze({intersection, latest, selection, createToolbar});
})();
