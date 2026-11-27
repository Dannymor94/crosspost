/* T-218: server-side revisioned drafts, shared by React and Preact. */
(() => {
  "use strict";
  const blank = () => ({content_type:"post", text:"", article_title:"", article_text:"",
    link_phrase:"", targets:[], when:"now", scheduled_at:"", media_required:false});
  function normalize(value) {
    const result = {...blank(), ...(value || {})};
    result.targets = [...new Set(result.targets)].sort();
    return result;
  }
  const serialize = value => JSON.stringify(normalize(value));
  const leaveChecks = new Set();
  function canLeave() {
    return ![...leaveChecks].some(check => check()) ||
      window.confirm("Есть несохранённый черновик. Покинуть редактор?");
  }
  async function request(base, actor, signal, body) {
    const headers = {"X-Draft-Actor": String(actor)};
    const options = {credentials:"same-origin", cache:"no-store", signal, headers};
    if (body !== undefined) {
      options.method = "PUT";
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(base + "/editor-draft", options);
    if (!response.ok) {
      const e = new Error("Черновик не сохранён.");
      e.status = response.status;
      throw e;
    }
    const row = await response.json();
    if (!row || !/^[0-9a-f]{64}$/.test(row.scope) ||
      !(row.revision === null || /^[0-9a-f]{32}$/.test(row.revision)))
      throw new Error("Invalid draft response");
    return row;
  }
  const confirmed = outcomes => Array.isArray(outcomes) && outcomes.length > 0 && outcomes.every(o => o.status === "done");
  function useDraft({useState,useRef,useEffect}, {base, actor, data, hydrate, paused, mediaIdentity}) {
    const [status,setStatus] = useState("loading");
    const [loaded,setLoaded] = useState(false);
    const [saved,setSaved] = useState(null);
    const [attempt,setAttempt] = useState(0);
    const receipt = useRef(null);
    const generation = useRef(null);
    const latest = useRef("");
    const latestMedia = useRef(null);
    latestMedia.current = mediaIdentity;
    const snapshot = serialize(data);
    latest.current = snapshot;
    const dirty = loaded && snapshot !== saved;
    const dirtyRef = useRef(false);
    dirtyRef.current = dirty;
    useEffect(() => {
      const controller = new AbortController();
      generation.current = controller;
      setLoaded(false); setStatus("loading"); setSaved(null); receipt.current = null;
      request(base, actor, controller.signal).then(row => {
        if (controller.signal.aborted) return;
        receipt.current = row;
        const value = normalize(row.data);
        hydrate(value);
        setSaved(serialize(value));
        setLoaded(true);
        setStatus(row.operation_state === "rejected" ? "rejected" :
          row.operation_state === "recorded" ? "recorded" :
          row.operation_state === "used" ? "consumed" : row.data ? "saved" : "ready");
      }).catch(e => {
        if (!controller.signal.aborted) setStatus(e.status === 401 || e.status === 403 ? "auth" : "load-error");
      });
      return () => { controller.abort(); receipt.current = null; };
    }, [base,actor,attempt]);
    useEffect(() => {
      if (!loaded || !dirty || paused || !receipt.current ||
          ["saving","conflict","save-error","auth","loading","recorded","consumed","rejected"].includes(status)) return;
      const controller = generation.current;
      const timer = setTimeout(async () => {
        if (!controller || controller.signal.aborted || latest.current !== snapshot) return;
        const prior = receipt.current;
        setStatus("saving");
        try {
          const row = await request(base,actor,controller.signal,{
            scope:prior.scope, revision:prior.revision, data:JSON.parse(snapshot)
          });
          if (controller.signal.aborted || row.scope !== prior.scope) return;
          receipt.current = row;
          setSaved(snapshot);
          setStatus("saved");
        } catch(e) {
          if (!controller.signal.aborted)
            setStatus(e.status === 409 ? "conflict" : e.status === 401 || e.status === 403 ? "auth" : "save-error");
        }
      }, 450);
      return () => clearTimeout(timer);
    }, [base,actor,loaded,snapshot,saved,paused,status]);
    useEffect(() => {
      const check = () => dirtyRef.current;
      const onUnload = e => { if (check()) { e.preventDefault(); e.returnValue = ""; } };
      leaveChecks.add(check); window.addEventListener("beforeunload",onUnload);
      return () => { leaveChecks.delete(check); window.removeEventListener("beforeunload",onUnload); };
    }, []);
    function capture() {
      if (!loaded || dirty || !receipt.current?.revision || !["ready","saved"].includes(status)) return null;
      return {scope:receipt.current.scope, revision:receipt.current.revision, snapshot, media:latestMedia.current, controller:generation.current};
    }
    function unchanged(token) {
      return !!token && token.controller === generation.current && !token.controller.signal.aborted && latest.current === token.snapshot && latestMedia.current === token.media;
    }
    async function clearSubmitted(token) {
      if (!unchanged(token) || receipt.current?.scope !== token.scope || receipt.current?.revision !== token.revision) return false;
      setStatus("saving");
      try {
        const row = await request(base,actor,token.controller.signal,{scope:token.scope,revision:token.revision,data:null});
        if (token.controller.signal.aborted || row.scope !== token.scope) return false;
        receipt.current = row;
        const same = unchanged(token);
        if (same) hydrate(blank());
        setSaved(serialize(blank()));
        setStatus("saved");
        return same;
      } catch(e) {
        if (!token.controller.signal.aborted) setStatus(e.status === 409 ? "conflict" : e.status === 401 || e.status === 403 ? "auth" : "save-error");
        return false;
      }
    }
    function markRecorded(token) {
      if (receipt.current?.scope === token.scope && receipt.current?.revision === token.revision &&
          token.controller === generation.current) setStatus("recorded");
    }
    function markRecovered(result) {
      if (!result?.known || receipt.current?.revision !== result.id) return false;
      setStatus(result.rejected ? "rejected" : "recorded");
      return true;
    }
    function renewRejected() {
      if (status !== "rejected") return;
      setSaved(null); setStatus("ready");
    }
    async function startNew() {
      if (status !== "recorded" || !receipt.current ||
          !window.confirm("Создать новый пустой пост? Сохранённый снимок предыдущего поста будет закрыт.")) return false;
      const prior = receipt.current, controller = generation.current;
      if (!controller || controller.signal.aborted) return false;
      setStatus("saving");
      try {
        const row = await request(base,actor,controller.signal,{scope:prior.scope,revision:prior.revision,data:null});
        if (controller.signal.aborted || row.scope !== prior.scope) return false;
        receipt.current = row;
        hydrate(blank()); setSaved(serialize(blank())); setStatus("saved");
        return true;
      } catch(e) {
        if (!controller.signal.aborted) setStatus(e.status === 409 ? "conflict" : "recorded");
        return false;
      }
    }
    const reload = () => {
      if (dirty && !window.confirm("Несохранённые изменения будут потеряны. Загрузить сохранённый черновик?")) return;
      generation.current?.abort();
      setAttempt(n => n + 1);
    };
    const retry = () => { if (status === "save-error") setStatus("ready"); else reload(); };
    const label = status === "conflict" ? "Черновик изменён в другой вкладке. Ваши изменения не сохранены."
      : status === "auth" ? "Сессия недействительна. Войдите снова."
      : status === "load-error" ? "Не удалось загрузить черновик."
      : status === "save-error" ? "Не удалось сохранить черновик."
      : status === "loading" ? "Загрузка черновика…"
      : status === "saving" ? "Сохраняем черновик…"
      : status === "rejected" ? "Отправка не началась. Продолжите редактирование."
      : status === "recorded" ? "Этот снимок уже использован. Создайте новый пост отдельно."
      : status === "consumed" ? "Результат этого снимка неизвестен. Повторная отправка заблокирована."
      : dirty ? "Есть несохранённые изменения"
      : status === "saved" ? "Черновик сохранён" : "Нет сохранённого черновика";
    return {loaded,dirty,status,label,retry,reload,capture,unchanged,clearSubmitted,markRecorded,markRecovered,renewRejected,startNew,canSend:loaded&&!dirty&&["ready","saved"].includes(status)};
  }
  window.CrosspostDraft = Object.freeze({useDraft,canLeave,normalize,confirmed});
})();
