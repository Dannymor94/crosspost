/* History is a read-only projection of persisted observations, not a send receipt. */
(() => {
  "use strict";
  const LABELS=Object.freeze({
    scheduled:"Запланировано",pending_approval:"На проверке",publishing:"Публикуется",
    cancelled:"Отменено",rejected:"Отклонено",done_observed:"Есть подтверждённые результаты",
    submitted_observed:"Отправлено без подтверждения",failed_observed:"Зафиксирован отказ",
    needs_relogin_observed:"Нужен повторный вход",mixed_observed:"Разные результаты по площадкам",
    in_progress_observed:"Есть незавершённые попытки",unknown:"Результат неизвестен",
    done:"Опубликовано",submitted:"Отправлено без подтверждения",failed:"Не опубликовано",
    needs_relogin:"Нужен повторный вход",new:"Ожидает",queued:"В очереди",attempting:"Выполняется"
  });
  const label=status=>Object.hasOwn(LABELS,status)?LABELS[status]:LABELS.unknown;
  const rowKey=e=>e.source+":"+e.key;
  function validate(data) {
    if(data?.scope!=="scheduled_and_observed_immediate"||!Array.isArray(data.entries)||
       !(data.next_cursor===null||typeof data.next_cursor==="string"&&data.next_cursor.length>0&&data.next_cursor.length<=512)||
       data.entries.some(e=>!e||!["scheduled","immediate"].includes(e.source)||
         typeof e.key!=="string"||!e.key||typeof e.created_at_utc!=="string"||
         !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(e.created_at_utc)||
         !Number.isFinite(Date.parse(e.created_at_utc))||
         !(e.text===null||typeof e.text==="string")||typeof e.status!=="string"||
         e.coverage!=="observed_only"||!Array.isArray(e.outcomes)||
         e.outcomes.some(o=>!o||typeof o.channel!=="string"||typeof o.target!=="string"||typeof o.status!=="string"))||
       new Set(data.entries.map(rowKey)).size!==data.entries.length) throw Error("Invalid history");
    return data;
  }
  // Recent persisted observations only. An absent observation is not success,
  // and a submitted observation is never a confirmed delivery or retry prompt.
  const ATTENTION=Object.freeze({
    failed:"Зафиксирован отказ",needs_relogin:"Нужен повторный вход",
    submitted:"Отправлено без подтверждения",unknown:"Результат неизвестен",
    banned:"Ограничено площадкой",deferred:"Отложено площадкой"
  });
  function attention(data) {
    return validate(data).entries.flatMap(e=>{
      const outcomes=e.outcomes.filter(o=>Object.hasOwn(ATTENTION,o.status));
      if(!outcomes.length&&e.status!=="rejected"&&e.status!=="unknown") return [];
      return [{key:rowKey(e),when:e.created_at_utc,text:e.text,
        rejected:e.status==="rejected",unknown:e.status==="unknown",
        outcomes:outcomes.map(o=>({channel:o.channel,target:o.target,label:ATTENTION[o.status]}))}];
    });
  }
  function useAttention({useState,useEffect},pid) {
    const [attempt,setAttempt]=useState(0),[result,setResult]=useState(null);
    useEffect(()=>{
      if(!Number.isSafeInteger(pid)||pid<=0)return;
      const controller=new AbortController();
      setResult({pid,attempt,kind:"loading"});
      fetch("/api/profiles/"+pid+"/history?limit=20",{credentials:"same-origin",cache:"no-store",signal:controller.signal})
        .then(r=>{if(!r.ok)throw Error("Read failed");return r.json();})
        .then(data=>{if(!controller.signal.aborted)setResult({pid,attempt,kind:"ready",items:attention(data)});})
        .catch(()=>{if(!controller.signal.aborted)setResult({pid,attempt,kind:"error"});});
      return ()=>controller.abort();
    },[pid,attempt]);
    return {...(!pid?{kind:"no-profile"}:result?.pid===pid&&result.attempt===attempt?result:{kind:"loading"}),
      retry:()=>setAttempt(x=>x+1)};
  }
  function create(h,{useState,useEffect,useRef}) {
    return function HistoryPanel({pid,onReuseText}) {
      const [state,setState]=useState(null), [attempt,setAttempt]=useState(0);
      const [filter,setFilter]=useState("all"),[query,setQuery]=useState(""),[activeQuery,setActiveQuery]=useState("");
      const flight=useRef(null);
      async function load(cursor,previous) {
        if(flight.current) flight.current.abort();
        const controller=new AbortController();flight.current=controller;
        const scope={pid,attempt,filter,activeQuery};
        setState({...scope,kind:cursor?"more":"loading",entries:previous||[],cursor});
        try {
          const base="/api/profiles/"+pid+"/history";
          const filtered=filter!=="all"||activeQuery!=="";
          const url=filtered?base+"/search":base+"?limit=20"+(cursor?"&before="+encodeURIComponent(cursor):"");
          const options={credentials:"same-origin",cache:"no-store",signal:controller.signal};
          if(filtered) Object.assign(options,{method:"POST",headers:{"Content-Type":"application/json"},
            body:JSON.stringify({source:filter,query:activeQuery,limit:20,before:cursor||null})});
          const response=await fetch(url,options);
          if(!response.ok) throw Error("Read failed");
          const data=validate(await response.json());
          if(controller.signal.aborted) return;
          const entries=[...(previous||[]),...data.entries];
          if(new Set(entries.map(rowKey)).size!==entries.length||
             (cursor&&data.next_cursor===cursor)) throw Error("Invalid page");
          setState({...scope,kind:"ready",entries,cursor:data.next_cursor});
        } catch(_) {
          if(!controller.signal.aborted) setState({...scope,kind:cursor?"more-error":"error",entries:previous||[],cursor});
        }
      }
      useEffect(()=>{
        if(Number.isSafeInteger(pid)&&pid>0) load(null,[]);
        return ()=>flight.current?.abort();
      },[pid,attempt,filter,activeQuery]);
      const current=state?.pid===pid&&state.attempt===attempt&&state.filter===filter&&state.activeQuery===activeQuery?state:null;
      const entries=current?.entries||[];
      function submitSearch(e) {
        e.preventDefault();
        const next=query.trim();
        if(next===activeQuery)setAttempt(x=>x+1);else setActiveQuery(next);
      }
      function row(e) {
        return h("article",{className:"cp-panel cp-stack",key:rowKey(e)},
          h("div",{className:"cp-row"},h("strong",null,label(e.status)),
            h("span",{className:"cp-muted"},e.source==="scheduled"?"Из расписания":"Немедленная")),
          h("small",{className:"cp-muted"},"Создано: "+e.created_at_utc.slice(0,16).replace("T"," ")+" UTC"),
          h("p",{className:"cp-preview-text"},e.text===null?"Текст не был сохранён в истории":e.text||"Без текста"),
          onReuseText&&typeof e.text==="string"&&e.text.trim()&&e.text.length<=200000?h("button",{type:"button",className:"cp-button",onClick:()=>onReuseText(e.text)},"Использовать текст"):null,
          e.outcomes.length?h("details",null,h("summary",null,"Результаты по площадкам ("+e.outcomes.length+")"),
            ...e.outcomes.map((o,i)=>h("div",{key:i,className:"cp-history-outcome"},
              h("strong",null,o.channel),o.target?h("small",null,o.target):null,h("span",null,label(o.status))))):
            h("p",{className:"cp-muted"},"Поканальных результатов пока нет."));
      }
      return h("section",{className:"cp-stack"},
        h("h1",{className:"cp-title"},"История"),
        h("p",{className:"cp-muted"},"Записи расписания и сохранённые результаты немедленных публикаций. Неполные результаты не означают, что весь пост опубликован."),
        h("div",{className:"cp-panel cp-stack"},
          h("button",{className:"cp-button",onClick:()=>setAttempt(x=>x+1)},"Обновить историю"),
          h("select",{className:"cp-input","aria-label":"Тип публикации",value:filter,onChange:e=>setFilter(e.target.value)},
            h("option",{value:"all"},"Все"),h("option",{value:"scheduled"},"Из расписания"),h("option",{value:"immediate"},"Немедленные")),
          h("form",{className:"cp-stack",onSubmit:submitSearch},
            h("input",{className:"cp-input","aria-label":"Поиск по сохранённым записям",placeholder:"Текст поста или код площадки",maxLength:120,value:query,onInput:e=>setQuery(e.target.value)}),
            h("div",{className:"cp-row"},
              h("button",{type:"submit",className:"cp-button"},"Найти"),
              h("button",{type:"button",className:"cp-button",onClick:()=>{setQuery("");setActiveQuery("");setFilter("all");}},"Сбросить поиск")),
            h("p",{className:"cp-muted"},"Поиск по всем сохранённым страницам, с учётом регистра. Текст старых немедленных публикаций может отсутствовать.")),
          !pid?h("p",null,"Выберите профиль."):!current||current.kind==="loading"?h("p",{role:"status"},"Загружаем историю…"):
          current.kind==="error"?h("p",{role:"alert"},"Не удалось загрузить историю."):
          h("div",{className:"cp-stack"},
            entries.length?entries.map(row):h("p",{className:"cp-muted"},filter!=="all"||activeQuery?"Совпадений нет.":"Сохранённых записей нет."),
            current.kind==="more-error"?h("p",{role:"alert"},"Не удалось загрузить следующую страницу."):null,
            current.cursor?h("button",{className:"cp-button",disabled:current.kind==="more",onClick:()=>load(current.cursor,entries)},current.kind==="more"?"Загрузка…":"Показать ещё"):null)));
    };
  }
  window.CrosspostHistoryView=Object.freeze({create,validate,label,attention,useAttention});
})();
