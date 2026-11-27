/* Connection display is a snapshot, not an external availability guarantee. */
(() => {
  "use strict";
  const STATES=Object.freeze({
    live:{label:"Подключено",tone:"success",advice:"Последнее сохранённое состояние активно. Доступность площадки проверяется при публикации."},
    needs_relogin:{label:"Нужен повторный вход",tone:"warning",advice:"Откройте мастер повторного входа вместе с администратором."},
    not_connected:{label:"Не подключено",tone:"info",advice:"Подготовьте аккаунт и необходимые права для подключения."},
    banned:{label:"Ограничено площадкой",tone:"error",advice:"Не повторяйте отправку или вход автоматически. Обратитесь к администратору."},
    unknown:{label:"Состояние неизвестно",tone:"warning",advice:"Обновите данные. Если состояние не уточняется, обратитесь к администратору."}
  });
  const state=value=>Object.hasOwn(STATES,value)?value:"unknown";
  const describe=value=>STATES[state(value)];
  const canManage=value=>["live","needs_relogin","not_connected"].includes(value);
  function rows(data) {
    if(!Array.isArray(data)||data.some(r=>!r||typeof r.channel!=="string"||!r.channel||
      typeof r.title!=="string"||typeof r.state!=="string")||
      new Set(data.map(r=>r.channel)).size!==data.length) throw Error("Invalid channel list");
    return data.map(r=>({channel:r.channel,title:r.title,state:state(r.state),
      expiresSoon:r.expires_soon===true,
      steps:Array.isArray(r.guide?.steps)?r.guide.steps
        .filter(x=>x&&x.optional!==true&&typeof x.title==="string")
        .map(x=>x.title):[]}));
  }
  // Only public instructions from the existing declaration; no field values or
  // alternative session-import instructions are projected into the client view.
  function preparation(guide) {
    if(!Array.isArray(guide?.steps)) return [];
    return guide.steps.filter(x=>x&&x.optional!==true&&x.kind!=="tg_session"&&typeof x.title==="string")
      .map(x=>{
        const browser=x.kind==="browser_open"||x.kind==="browser_confirm";
        const body=browser?"Этот шаг выполняется в окне мастера с администратором. На телефоне окно серверного браузера может быть недоступно.":typeof x.body==="string"?x.body:"";
        let link="";
        try {
          const u=new URL(x.link);
          if(u.protocol==="https:"&&!u.username&&!u.password) link=u.href;
        } catch(_) {}
        return {title:x.title,body,landmark:!browser&&typeof x.landmark==="string"?x.landmark:"",
          link,linkLabel:typeof x.link_label==="string"&&x.link_label?x.link_label:"Открыть официальный раздел"};
      });
  }
  const needsAttention=r=>["needs_relogin","banned","unknown"].includes(r.state)||(r.state==="live"&&r.expiresSoon);
  function useConnections({useState,useEffect},pid) {
      const [attempt,setAttempt]=useState(0),[result,setResult]=useState(null);
      useEffect(()=>{
        if(!Number.isSafeInteger(pid)||pid<=0)return;
        const controller=new AbortController();
        setResult({pid,attempt,kind:"loading"});
        fetch("/api/profiles/"+pid+"/channels",{credentials:"same-origin",cache:"no-store",signal:controller.signal})
          .then(r=>{if(!r.ok)throw Error("Read failed");return r.json();})
          .then(data=>{if(!controller.signal.aborted)setResult({pid,attempt,kind:"ready",
            rows:rows(data).map((r,i)=>({...r,preparation:preparation(data[i].guide)}))});})
          .catch(()=>{if(!controller.signal.aborted)setResult({pid,attempt,kind:"error"});});
        return ()=>controller.abort();
      },[pid,attempt]);
      const current=result?.pid===pid&&result.attempt===attempt?result:null;
      return {...(!pid?{kind:"no-profile"}:current||{kind:"loading"}),retry:()=>setAttempt(x=>x+1)};
  }
  function create(h,hooks) {
    return function ConnectionPanel({pid}) {
      const current=useConnections(hooks,pid);
      const [filter,setFilter]=hooks.useState("all");
      const list=current.rows||[];
      const visible=filter==="attention"?list.filter(needsAttention):filter==="setup"?list.filter(r=>r.state==="not_connected"):list;
      return h("section",{className:"cp-stack"},
        h("h1",{className:"cp-title"},"Подключения"),
        h("p",{className:"cp-muted"},"Здесь видно состояние площадок вашего профиля. Подключение выполняется в защищённом мастере с администратором. Не пересылайте пароли, коды и токены в сообщениях."),
        h("div",{className:"cp-panel cp-stack"},
          h("button",{className:"cp-button",onClick:current.retry},"Обновить состояния"),
          h("label",null,"Показать площадки",
            h("select",{className:"cp-input",value:filter,onChange:e=>setFilter(e.target.value)},
              h("option",{value:"all"},"Все"),h("option",{value:"attention"},"Требуют внимания"),
              h("option",{value:"setup"},"Не подключены"))),
          current.kind==="ready"?h("p",{className:"cp-muted"},"Подключено: "+list.filter(r=>r.state==="live").length+
            " из "+list.length+" · Требуют внимания: "+list.filter(needsAttention).length):null),
        !pid?h("p",null,"Профиль пока не назначен."):!current||current.kind==="loading"?h("p",{role:"status"},"Загружаем подключения…"):
        current.kind==="error"?h("p",{role:"alert"},"Не удалось получить состояния подключений."):
        visible.length?visible.map(r=>{
          const info=describe(r.state);
          return h("article",{className:"cp-panel cp-stack",key:r.channel},
            h("div",{className:"cp-row"},h("strong",null,r.title),h("span",{className:"cp-status--"+info.tone},info.label)),
            h("p",{className:"cp-muted"},info.advice),
            r.expiresSoon&&r.state!=="banned"?h("p",{className:"cp-status--warning"},"Срок действия подключения скоро истечёт."):null,
            r.preparation.length&&canManage(r.state)?h("details",null,
              h("summary",null,"Этапы подключения"),
              h("p",{className:"cp-muted"},"Это инструкция, а не форма входа. Вводите секреты только в защищённом мастере. Не передавайте их администратору в переписке."),
              h("ol",{className:"cp-connection-steps"},...r.preparation.map((step,i)=>h("li",{key:i,className:"cp-stack"},
                h("strong",null,step.title),
                step.body?h("p",null,step.body):null,
                step.landmark?h("p",{className:"cp-muted"},step.landmark):null,
                step.link?h("a",{href:step.link,target:"_blank",rel:"noopener noreferrer"},step.linkLabel):null)))):null);
        }):h("p",null,filter==="all"?"Нет доступных площадок.":"Нет площадок в выбранной категории."));
    };
  }
  window.CrosspostConnections=Object.freeze({describe,canManage,rows,preparation,needsAttention,useConnections,create});
})();
