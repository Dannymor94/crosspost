/* T-517: explicit opt-in, browser-native install or manual instructions. */
(() => {
  "use strict";
  let deferred = null;
  let installed = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  window.addEventListener("beforeinstallprompt", event => {
    event.preventDefault();
    deferred = event;
  });
  window.addEventListener("appinstalled", () => {installed = true; deferred = null;});
  const ready = (async () => {
    if (!window.isSecureContext || !("serviceWorker" in navigator)) return false;
    try {
      const response = await fetch("/pwa/config", {credentials:"omit",cache:"no-store"});
      if (!response.ok) return false;
      const config = await response.json();
      if (config.enabled !== true) {
        const registration = await navigator.serviceWorker.getRegistration("/");
        if (registration && [registration.active,registration.waiting,registration.installing]
          .some(worker => worker && new URL(worker.scriptURL).pathname === "/pwa/sw.js")) {
          await registration.unregister();
          for (const key of await caches.keys()) {
            if (key.startsWith("crosspost-public-offline-")) await caches.delete(key);
          }
        }
        return false;
      }
      const manifest = document.createElement("link");
      manifest.rel = "manifest"; manifest.href = "/pwa/manifest.webmanifest";
      document.head.append(manifest);
      await navigator.serviceWorker.register("/pwa/sw.js",{scope:"/",updateViaCache:"none"});
      return true;
    } catch (_) {return false;}
  })();
  async function open() {
    if (!(await ready) || installed) return;
    if (deferred) {
      const prompt = deferred; deferred = null;
      try { await prompt.prompt(); await prompt.userChoice; return; } catch (_) {}
    }
    const dialog = document.createElement("dialog");
    dialog.setAttribute("aria-label","Установка Crosspost");
    dialog.style.cssText="max-width:min(90vw,28rem);padding:24px;border-radius:12px;line-height:1.5;color:inherit;background:var(--bg-card,#181d24);border:1px solid #536078";
    const title=document.createElement("h2");title.textContent="Установка на телефон";
    const note=document.createElement("p");note.textContent="iPhone: откройте Safari → Поделиться → На экран «Домой». Android: откройте меню Chrome → Установить приложение или Добавить на главный экран. Если пункта нет, используйте обычную вкладку.";
    const close=document.createElement("button");close.textContent="Закрыть";close.className="cp-button";close.onclick=()=>dialog.close();
    dialog.append(title,note,close);document.body.append(dialog);
    dialog.addEventListener("close",()=>dialog.remove(),{once:true});
    dialog.showModal();
  }
  function create(h,{useState,useEffect}) {
    return function InstallButton() {
      const [visible,setVisible]=useState(false);
      useEffect(()=>{
        let active=true;
        const hide=()=>setVisible(false);
        ready.then(enabled=>{if(active)setVisible(enabled&&!installed);});
        window.addEventListener("appinstalled",hide);
        return ()=>{active=false;window.removeEventListener("appinstalled",hide);};
      },[]);
      return visible?h("button",{type:"button",className:"cp-button",onClick:open},"Установить приложение"):null;
    };
  }
  window.CrosspostPwa=Object.freeze({ready,open,create});
})();
