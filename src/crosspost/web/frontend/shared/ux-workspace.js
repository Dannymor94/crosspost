/* Framework-neutral hook for the host-owned navigation. No authorization decisions. */
(() => {
  "use strict";
  function useMenu({useRef,useEffect}, items, active, onNavigate, label, mobile = false) {
    const root = useRef(null), instance = useRef(null), callback = useRef(onNavigate);
    callback.current = onNavigate;
    useEffect(() => {
      if (!root.current) return;
      const menu = window.CrosspostNavigation.mount(root.current, {
        items, active, label, onNavigate: id => callback.current(id)
      });
      if (mobile) root.current.querySelector("nav").classList.add("cp-mobile-nav");
      instance.current = menu;
      return () => { menu.dispose(); instance.current = null; };
    }, [items, label, mobile]);
    useEffect(() => { instance.current?.setActive(active); }, [active]);
    return root;
  }
  // A fresh in-memory epoch per authenticated mount; never restore a profile or
  // account from browser history. The URL and sensitive draft data are unchanged.
  function useHistory({useRef,useEffect}, enabled, initial, allowed, onRoute) {
    const current = useRef(initial), session = useRef(null), callback = useRef(onRoute);
    callback.current = onRoute;
    function write(method, entry) { window.history[method]({crosspostWorkspace:entry}, ""); }
    function reset(next) {
      current.current = next;
      if (!session.current) return;
      const entry = {epoch:crypto.getRandomValues(new Uint32Array(4)).join("-"), index:0, view:next};
      session.current = {entry, restoring:null};
      write("replaceState",entry);
    }
    useEffect(() => {
      if (!enabled) return;
      session.current = {};
      reset(current.current);
      function pop(event) {
        const owner = session.current;
        if (!owner) return;
        const target = event.state?.crosspostWorkspace, from = owner.entry;
        if (target?.epoch !== from.epoch || !allowed.includes(target.view) ||
            !Number.isSafeInteger(target.index)) {
          // Entry belongs to an earlier mount/profile. Do not revive it.
          reset(current.current);
          return;
        }
        if (owner.restoring !== null && target.index === owner.restoring) {
          owner.restoring = null;
          return;
        }
        if (target.view !== current.current && !window.CrosspostDraft.canLeave()) {
          const delta = from.index - target.index;
          if (delta) {
            owner.restoring = from.index;
            window.history.go(delta);
          } else write("replaceState",from);
          return;
        }
        owner.entry = target;
        owner.restoring = null;
        current.current = target.view;
        callback.current(target.view);
      }
      window.addEventListener("popstate",pop);
      return () => { window.removeEventListener("popstate",pop); session.current = null; };
    }, [enabled]);
    function navigate(next) {
      if (!allowed.includes(next)) return false;
      if (current.current === next) return true;
      if (session.current?.restoring !== null && session.current) return false;
      if (!window.CrosspostDraft.canLeave()) return false;
      const owner = session.current;
      if (owner) {
        const entry = {...owner.entry,index:owner.entry.index+1,view:next};
        write("pushState",entry);
        owner.entry = entry;
      }
      current.current = next;
      callback.current(next);
      return true;
    }
    function replaceContext(next) { reset(next); callback.current(next); }
    return {navigate,replaceContext};
  }
  window.CrosspostWorkspace = Object.freeze({useMenu,useHistory});
})();
