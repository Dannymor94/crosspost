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
  window.CrosspostWorkspace = Object.freeze({useMenu});
})();
