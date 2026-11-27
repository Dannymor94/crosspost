/* Read-only active schedule summary. No writes, no inferred historical timezone. */
(() => {
  "use strict";
  const validStatus = status => ["scheduled","pending_approval"].includes(status);
  function summarize(rows) {
    if (!Array.isArray(rows) || rows.some(r=>!r || !Number.isSafeInteger(r.id) ||
        !validStatus(r.status) || typeof r.text !== "string" ||
        typeof r.scheduled_at !== "string" || !r.scheduled_at ||
        !["utc","legacy_local"].includes(r.time_basis))) throw Error("Invalid schedule");
    if (new Set(rows.map(r=>r.id)).size !== rows.length) throw Error("Duplicate schedule");
    return {scheduled:rows.filter(r=>r.status==="scheduled").length,
      pending:rows.filter(r=>r.status==="pending_approval").length,
      rows:rows.map(r=>({id:r.id,status:r.status,text:r.text,when:r.scheduled_at,basis:r.time_basis,
        channels:Array.isArray(r.channels)?r.channels.map(x=>typeof x==="string"?x:x?.channel).filter(x=>typeof x==="string"):[]}))};
  }
  function timeLabel(row,zone) {
    if (row.basis==="legacy_local") return row.when.slice(0,16).replace("T"," ")+" · старое время сервера";
    if (!/(Z|[+-]\d\d:\d\d)$/.test(row.when) || !Number.isFinite(Date.parse(row.when))) return "Время неизвестно";
    try { return window.CrosspostTime.label(row.when,zone); } catch (_) { return row.when; }
  }
  async function load(pid,signal) {
    const base="/api/profiles/"+pid;
    const get=async path=>{
      const response=await fetch(base+path,{credentials:"same-origin",cache:"no-store",signal});
      if(!response.ok) throw Error("Read failed");
      return response.json();
    };
    const [rows,zone]=await Promise.all([get("/scheduled"),get("/settings/timezone").then(x=>x.zone).catch(()=>null)]);
    return {...summarize(rows),zone:typeof zone==="string"?zone:null};
  }
  function useOverview({useState,useEffect},pid) {
    const [attempt,setAttempt]=useState(0), [state,setState]=useState(null);
    useEffect(()=>{
      if (!Number.isSafeInteger(pid) || pid<=0) return;
      const controller=new AbortController();
      setState({pid,attempt,kind:"loading"});
      load(pid,controller.signal).then(data=>{
        if(!controller.signal.aborted) setState({pid,attempt,kind:"ready",data});
      }).catch(()=>{
        if(!controller.signal.aborted) setState({pid,attempt,kind:"error"});
      });
      return ()=>controller.abort();
    },[pid,attempt]);
    return {...(!pid?{kind:"no-profile"}:state?.pid===pid&&state.attempt===attempt?state:{kind:"loading"}),
      retry:()=>setAttempt(v=>v+1)};
  }
  window.CrosspostOverview=Object.freeze({summarize,timeLabel,useOverview});
})();
